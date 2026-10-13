"""Install verified audio.cpp releases into the program's own data directory."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
import zipfile

import httpx

from .config import atomic_json, file_digest
from .downloads import DownloadPaused, download_file
from .errors import VoiceError
from .engine_validation import CUDA_DLLS, clear_binary_cache, inspect_engine
from .engine_updates import fetch_release, version_key


class EngineManager:
    def __init__(self, models, catalog=None, client_factory=None):
        self.models, self.manager, self.config = models, models.manager, models.config
        self.root = self.manager.root / "runtime"
        self.root.mkdir(exist_ok=True)
        self.download_dir = self.root / "downloads"
        self.download_dir.mkdir(exist_ok=True)
        self.catalog = catalog or json.loads((Path(__file__).parent / "assets" / "engine_catalog.json").read_text(encoding="utf-8"))
        self.entries = {e["id"]: e for e in self.catalog["backends"]}
        self.recommended = self.catalog.get("recommended", self.catalog)
        self.update_error = ""
        self.checking_updates = False
        self.client_factory = client_factory or (lambda: httpx.Client(follow_redirects=True, trust_env=False,
                                                 timeout=httpx.Timeout(15, connect=10, read=10)))
        self.lock = threading.RLock()
        self.states, self.threads, self.cancel = {}, {}, {}
        self.validation_cache = {}
        self.closed = False

    def check_updates(self):
        with self.lock:
            if self.checking_updates or any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("DOWNLOAD_BUSY", "请等待引擎安装结束后检查更新", stage="引擎更新")
            self.checking_updates = True
        try:
            candidate = fetch_release(self.client_factory)
            with self.lock:
                if version_key(candidate["version"]) >= version_key(self.recommended["version"]):
                    self.recommended = candidate
                self.update_error = ""
        except VoiceError as exc:
            self.update_error = str(exc)
            raise
        finally:
            self.checking_updates = False
        return self.listing()

    def rollback(self, backend):
        with self.manager.lock, self.lock, self.config.lock:
            if any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("DOWNLOAD_BUSY", "引擎安装结束后才能回退", stage="引擎回退")
            value = self.config.read()
            previous = value.get("native_engine_previous", {}).get(backend)
            if not previous or not self._inspect(previous["path"], backend)["valid"]:
                raise VoiceError("ENGINE_ROLLBACK_MISSING", "旧引擎不存在或验证失败", stage="引擎回退")
            current = value.get("native_engines", {}).get(backend, {})
            self.models.set_runtime(previous["path"], backend, previous.get("version"))
            value = self.config.read()
            value.setdefault("native_engines", {})[backend] = previous
            value.setdefault("native_engine_previous", {})[backend] = current
            atomic_json(self.config.path, value)
            self.states.pop(backend, None)
        return self.listing()

    def _inspect(self, path, backend):
        try:
            signature = tuple(sorted((p.name.lower(), p.stat().st_size, p.stat().st_mtime_ns)
                                     for p in Path(path).parent.iterdir() if p.is_file() and p.suffix.lower() in (".dll", ".exe")))
        except OSError:
            signature = ()
        key = (str(path), backend)
        previous = self.validation_cache.get(key)
        if previous is None or previous[0] != signature:
            self.validation_cache[key] = (signature, inspect_engine(path, backend))
        return self.validation_cache[key][1]

    def _find(self, backend):
        value = self.config.read()
        registered = value.get("native_engines", {}).get(backend, {})
        candidates = [registered.get("path"), self.models.runtime()]
        candidates += [v.get("engine_executable") for v in self.config.voices() if v.get("backend") == "audio_cpp"]
        best = None
        for path in dict.fromkeys(p for p in candidates if p):
            result = self._inspect(path, backend)
            if best is None or sum(c["valid"] for c in result["components"]) > sum(c["valid"] for c in best["components"]):
                best = result
            if result["valid"]:
                break
        return best or {**inspect_engine("", backend), "path": "", "issues": ["尚未配置本地引擎"]}

    def verify(self, backend):
        if backend not in self.entries:
            raise VoiceError("ENGINE_BACKEND_UNSUPPORTED", "请选择 NVIDIA CUDA 或 CPU 引擎", stage="引擎验证")
        with self.lock:
            self.validation_cache.clear()
            clear_binary_cache()
            result = self._find(backend)
        return {"verification": result, "engine": self.listing()}

    def listing(self):
        runtime = self.models.runtime()
        saved = self.config.read().get("native_engines", {})
        options = []
        with self.lock:
            for backend, entry in self.entries.items():
                installed = saved.get(backend, {})
                if installed.get("version") == self.recommended["version"]:
                    entry = next(e for e in self.recommended["backends"] if e["id"] == backend)
                validation = self._find(backend)
                path = validation["path"] if validation["valid"] or any(c["valid"] for c in validation["components"]) else installed.get("path", "")
                exists = validation["valid"]
                total = sum(a["size"] for a in entry["archives"])
                received = sum(self._received(a) for a in entry["archives"])
                state = self.states.get(backend, {"status": "done" if exists else "paused" if received else "available",
                                                  "received": received, "error": ""})
                if state["status"] == "done" and not exists:
                    state = {**state, "status": "available"}
                archives = []
                for index, archive in enumerate(entry["archives"]):
                    component = validation["components"][min(index, len(validation["components"]) - 1)]
                    owned = component["valid"]
                    amount = archive["size"] if owned else self._received(archive)
                    current = state.get("archive") == archive["name"]
                    cached = (self.download_dir / archive["name"]).is_file()
                    if owned:
                        status = "done"
                    elif current and state["status"] in ("downloading", "verifying", "pausing"):
                        status = state["status"]
                    elif cached:
                        status = "downloaded"
                    elif current and state["status"] == "failed":
                        status = "failed"
                    elif amount == archive["size"]:
                        status = "pending_verification"
                    else:
                        status = "paused" if amount else "available"
                    archives.append({"name": archive["name"], "size": archive["size"],
                                     "received": amount, "status": status, "verified": owned, "issues": component["issues"]})
                actual_version = installed.get("version") or (self.config.read().get("native_engine_version") if path == runtime else None)
                previous = self.config.read().get("native_engine_previous", {}).get(backend, {})
                target = self.recommended
                update_available = exists and version_key(target["version"]) > version_key(actual_version)
                if state.get("upgrade"):
                    target_entry = next((e for e in target["backends"] if e["id"] == backend), entry)
                    total = sum(a["size"] for a in target_entry["archives"])
                    archives = [{**a, "received": a["size"] if a["name"] in state.get("reused_archives", []) else self._received(a),
                                 "verified": a["name"] in state.get("reused_archives", []),
                                 "status": "done" if a["name"] in state.get("reused_archives", []) else state["status"] if state.get("archive") == a["name"] else "downloaded" if (self.download_dir / a["name"]).is_file() else "available"}
                                for a in target_entry["archives"]]
                options.append({"backend": backend, "name": entry["name"], "description": entry["description"],
                                "version": actual_version or self.catalog["version"], "size": total, "installed": exists,
                                "path": path, "archives": archives, "verification": validation,
                                "installed_version": actual_version, "recommended_version": target["version"],
                                "update_available": update_available,
                                "rollback_version": previous.get("version"), "can_rollback": bool(previous.get("path") and Path(previous["path"]).is_file()
                                    and (not previous.get("version") or version_key(previous["version"]) < version_key(actual_version))), **state})
        value = self.config.read()
        active_backend = value.get("native_engine_backend") or next((v.get("device", "cuda") for v in self.config.voices() if v.get("engine_executable") == runtime), "cuda")
        return {"ready": bool(runtime and self._inspect(runtime, active_backend)["valid"]), "path": runtime,
                "directory": str(self.root), "supported": platform.system() == "Windows" and platform.machine().lower() in ("amd64", "x86_64"),
                "options": options, "version": value.get("native_engine_version"),
                "release": self.recommended.get("release"), "update_error": self.update_error,
                "checking_updates": self.checking_updates}

    def _received(self, archive):
        try:
            return min(archive["size"], (self.download_dir / (archive["name"] + ".part")).stat().st_size)
        except FileNotFoundError:
            return archive["size"] if (self.download_dir / archive["name"]).is_file() else 0

    def start(self, backend, upgrade=False):
        if backend not in self.entries:
            raise VoiceError("ENGINE_BACKEND_UNSUPPORTED", "请选择 NVIDIA CUDA 或 CPU 引擎", stage="引擎下载")
        if not self.listing()["supported"]:
            raise VoiceError("ENGINE_PLATFORM_UNSUPPORTED", "在线安装暂支持 Windows 64 位系统", stage="引擎下载")
        with self.lock:
            if self.closed or self.manager.stopping.is_set():
                raise VoiceError("SERVICE_STOPPING", "服务正在停止", stage="引擎下载")
            if self.checking_updates or any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("DOWNLOAD_BUSY", "已有推理引擎正在下载或安装，请等待或暂停", stage="引擎下载")
            event = threading.Event()
            self.cancel[backend] = event
            release = self.recommended if upgrade else self.catalog
            self.states[backend] = {"status": "downloading", "received": 0, "error": "", "upgrade": upgrade, "target_version": release["version"]}
            thread = threading.Thread(target=self._install, args=(backend, event, release, upgrade), name="engine-download", daemon=True)
            self.threads[backend] = thread
            thread.start()
            return dict(self.states[backend])

    def pause(self, backend):
        with self.lock:
            if backend in self.cancel and self.threads[backend].is_alive():
                self.cancel[backend].set()
                self.states[backend]["status"] = "pausing"
        return {"paused": backend}

    def _state(self, backend, **changes):
        with self.lock:
            self.states[backend].update(changes)

    def use(self, backend):
        result = self._find(backend)
        if not result["valid"]:
            raise VoiceError("ENGINE_MISSING", "引擎验证未通过：" + "；".join(result["issues"]), stage="引擎配置")
        installed = self.config.read().get("native_engines", {}).get(backend, {})
        return self.models.set_runtime(result["path"], backend=backend, version=installed.get("version"))

    def _extract(self, archive, directory, event):
        with zipfile.ZipFile(archive) as package:
            if len(package.infolist()) > 10000 or sum(f.file_size for f in package.infolist()) > 8 * 1024**3:
                raise ValueError("引擎压缩包内容大小异常")
            for member in package.infolist():
                if event.is_set():
                    raise DownloadPaused()
                relative = PurePosixPath(member.filename.replace("\\", "/"))
                target = directory.joinpath(*relative.parts)
                if relative.is_absolute() or ".." in relative.parts or ":" in member.filename or stat.S_ISLNK(member.external_attr >> 16) or not target.resolve().is_relative_to(directory.resolve()):
                    raise ValueError(f"引擎压缩包包含无效路径：{member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        if event.is_set():
                            raise DownloadPaused()
                        output.write(chunk)

    def _install(self, backend, event, release=None, upgrade=False):
        staging = None
        try:
            installed = self.config.read().get("native_engines", {}).get(backend, {})
            previous = self._find(backend)
            release = release or self.catalog
            entry = next(e for e in release["backends"] if e["id"] == backend)
            history = self.config.read().get("native_engine_previous", {}).get(backend, {})
            if upgrade and history.get("version") == release["version"] and self._inspect(history.get("path"), backend)["valid"]:
                self._probe(Path(history["path"]))
                with self.config.lock:
                    value = self.config.read()
                    current = value.get("native_engines", {}).get(backend, {})
                    value.setdefault("native_engines", {})[backend] = history
                    value.setdefault("native_engine_previous", {})[backend] = current
                    atomic_json(self.config.path, value)
                self._activate(backend, event, entry)
                return
            if previous["valid"] and (not upgrade or installed.get("version") == release["version"]):
                self._activate(backend, event)
                return
            staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
            owned = [dict(c) for c in previous["components"]]
            if upgrade:
                owned[0]["valid"] = False
            self._state(backend, reused_archives=[a["name"] for i,a in enumerate(entry["archives"]) if owned[min(i,len(owned)-1)]["valid"]])
            # Reuse validated local components. Never write into the original installation.
            source = Path(previous["path"]).parent
            if any(c["valid"] for c in owned):
                target = staging / "local"
                target.mkdir()
                cuda_names = {name.lower() for name in CUDA_DLLS}
                for item in source.iterdir():
                    runtime_file = item.name.lower() in cuda_names
                    keep = owned[1]["valid"] if runtime_file and backend == "cuda" else owned[0]["valid"]
                    if keep and item.is_file() and item.suffix.lower() in (".exe", ".dll"):
                        shutil.copy2(item, target / item.name)
                    elif keep and item.is_dir() and item.name == "model_specs":
                        shutil.copytree(item, target / item.name)
            completed = 0
            missing_archives = []
            for index, archive in enumerate(entry["archives"]):
                if owned[min(index, len(owned) - 1)]["valid"]:
                    completed += archive["size"]
                    continue
                missing_archives.append(archive)
                def progress(**changes):
                    if "received" in changes:
                        changes["received"] += completed
                    self._state(backend, archive=archive["name"], **changes)
                url = f"https://github.com/0xShug0/audio.cpp/releases/download/{release['version']}/{archive['name']}"
                cached = self.download_dir / archive["name"]
                try:
                    download_file(cached, archive, url, event, self.client_factory, progress)
                except ValueError:
                    # Complete archives are only our download cache; a corrupt one must not trap retries.
                    if cached.is_file():
                        cached.unlink()
                    raise
                completed += archive["size"]
            self._state(backend, status="extracting", received=completed)
            for archive in missing_archives:
                self._extract(self.download_dir / archive["name"], staging, event)
            executables = list(staging.rglob("audiocpp_server.exe"))
            if len(executables) != 1:
                raise ValueError("引擎压缩包需要包含唯一的 audiocpp_server.exe")
            executable = executables[0]
            with executable.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise ValueError("下载的推理引擎不是有效的 Windows 程序")
            for dll in list(staging.rglob("*.dll")):
                target = executable.parent / dll.name
                if dll != target:
                    if target.exists() and file_digest(str(dll)) != file_digest(str(target)):
                        raise ValueError(f"引擎包存在不同版本的同名 DLL：{dll.name}")
                    if not target.exists():
                        dll.replace(target)
            validation = inspect_engine(str(executable), backend)
            if not validation["valid"]:
                raise ValueError("引擎验证未通过：" + "；".join(validation["issues"]))
            if event.is_set():
                raise DownloadPaused()
            if upgrade:
                self._probe(executable)
            directory = self.root / f"{release['version']}-{backend}-{uuid.uuid4().hex[:8]}"
            relative = executable.relative_to(staging)
            staging.rename(directory)
            staging = None
            installed = {"path": str(directory / relative), "version": release["version"], "backend": backend}
            with self.config.lock:
                value = self.config.read()
                old = value.get("native_engines", {}).get(backend)
                if not old and previous["valid"]:
                    old = {"path": previous["path"], "version": value.get("native_engine_version"), "backend": backend}
                if old and old["path"] != installed["path"]:
                    value.setdefault("native_engine_previous", {})[backend] = old
                atomic_json(self.config.path, {**value, "native_engines": {**value.get("native_engines", {}), backend: installed}})
            self._activate(backend, event, entry)
        except DownloadPaused:
            self._state(backend, status="paused")
        except Exception as exc:
            self._state(backend, status="paused" if event.is_set() else "failed", error="" if event.is_set() else str(exc))
        finally:
            if staging and staging.resolve() != self.root.resolve() and staging.resolve().is_relative_to(self.root.resolve()) and staging.name.startswith(".install-"):
                shutil.rmtree(staging)

    def _probe(self, executable):
        from .runtime import external_environment
        with external_environment() as environment:
            result = subprocess.run([str(executable), "--help"], capture_output=True, timeout=30, env=environment,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("新引擎启动验证失败：" + (result.stderr or result.stdout).decode("utf-8", errors="replace")[-500:])

    def _activate(self, backend, event, entry=None):
        self._state(backend, status="waiting")
        while not event.is_set():
            with self.manager.lock:
                if self.manager.stopping.is_set():
                    raise DownloadPaused()
                if not self.manager.current and not getattr(self.manager, "editing_busy", False):
                    self.use(backend)
                    self._state(backend, status="done", error="", upgrade=False)
                    # Installed files are retained; complete ZIPs no longer need disk space.
                    for archive in (entry or self.entries[backend])["archives"]:
                        try:
                            (self.download_dir / archive["name"]).unlink(missing_ok=True)
                        except OSError:
                            pass
                    return
            event.wait(.25)
        raise DownloadPaused()

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancel.values():
                event.set()
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=20)
            if thread.is_alive():
                raise RuntimeError("推理引擎下载仍在退出中")
