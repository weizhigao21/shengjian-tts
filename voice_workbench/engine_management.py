"""Install verified audio.cpp releases into the program's own data directory."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile

import httpx

from .config import atomic_json, file_digest
from .downloads import DownloadPaused, download_file
from .errors import VoiceError


class EngineManager:
    def __init__(self, models, catalog=None, client_factory=None):
        self.models, self.manager, self.config = models, models.manager, models.config
        self.root = self.manager.root / "runtime"
        self.root.mkdir(exist_ok=True)
        self.download_dir = self.root / "downloads"
        self.download_dir.mkdir(exist_ok=True)
        self.catalog = catalog or json.loads((Path(__file__).parent / "assets" / "engine_catalog.json").read_text(encoding="utf-8"))
        self.entries = {e["id"]: e for e in self.catalog["backends"]}
        self.client_factory = client_factory or (lambda: httpx.Client(follow_redirects=True, trust_env=False,
                                                 timeout=httpx.Timeout(15, connect=10, read=10)))
        self.lock = threading.RLock()
        self.states, self.threads, self.cancel = {}, {}, {}
        self.closed = False

    def listing(self):
        runtime = self.models.runtime()
        saved = self.config.read().get("native_engines", {})
        options = []
        with self.lock:
            for backend, entry in self.entries.items():
                installed = saved.get(backend, {})
                path = installed.get("path", "")
                exists = bool(path and Path(path).is_file())
                total = sum(a["size"] for a in entry["archives"])
                received = sum(self._received(a) for a in entry["archives"])
                state = self.states.get(backend, {"status": "done" if exists else "paused" if received else "available",
                                                  "received": received, "error": ""})
                if state["status"] == "done" and not exists:
                    state = {**state, "status": "available"}
                options.append({"backend": backend, "name": entry["name"], "description": entry["description"],
                                "version": self.catalog["version"], "size": total, "installed": exists,
                                "path": path, **state})
        return {"ready": bool(runtime and Path(runtime).is_file()), "path": runtime,
                "directory": str(self.root), "supported": platform.system() == "Windows" and platform.machine().lower() in ("amd64", "x86_64"),
                "options": options}

    def _received(self, archive):
        try:
            return min(archive["size"], (self.download_dir / (archive["name"] + ".part")).stat().st_size)
        except FileNotFoundError:
            return archive["size"] if (self.download_dir / archive["name"]).is_file() else 0

    def start(self, backend):
        if backend not in self.entries:
            raise VoiceError("ENGINE_BACKEND_UNSUPPORTED", "请选择 NVIDIA CUDA 或 CPU 引擎", stage="引擎下载")
        if not self.listing()["supported"]:
            raise VoiceError("ENGINE_PLATFORM_UNSUPPORTED", "在线安装暂支持 Windows 64 位系统", stage="引擎下载")
        with self.lock:
            if self.closed or self.manager.stopping.is_set():
                raise VoiceError("SERVICE_STOPPING", "服务正在停止", stage="引擎下载")
            if any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("DOWNLOAD_BUSY", "已有推理引擎正在下载或安装，请等待或暂停", stage="引擎下载")
            event = threading.Event()
            self.cancel[backend] = event
            self.states[backend] = {"status": "downloading", "received": 0, "error": ""}
            thread = threading.Thread(target=self._install, args=(backend, event), name="engine-download", daemon=True)
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
        installed = self.config.read().get("native_engines", {}).get(backend, {})
        if not installed.get("path") or not Path(installed["path"]).is_file():
            raise VoiceError("ENGINE_MISSING", "该引擎尚未安装，请先下载", stage="引擎配置")
        return self.models.set_runtime(installed["path"], backend=backend, version=installed["version"])

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

    def _install(self, backend, event):
        staging = None
        try:
            installed = self.config.read().get("native_engines", {}).get(backend, {})
            if installed.get("version") == self.catalog["version"] and Path(installed.get("path") or ".").is_file():
                self._activate(backend, event)
                return
            entry = self.entries[backend]
            completed = 0
            for archive in entry["archives"]:
                def progress(**changes):
                    if "received" in changes:
                        changes["received"] += completed
                    self._state(backend, archive=archive["name"], **changes)
                url = f"https://github.com/0xShug0/audio.cpp/releases/download/{self.catalog['version']}/{archive['name']}"
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
            staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
            for archive in entry["archives"]:
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
            if event.is_set():
                raise DownloadPaused()
            directory = self.root / f"{self.catalog['version']}-{backend}-{uuid.uuid4().hex[:8]}"
            relative = executable.relative_to(staging)
            staging.rename(directory)
            staging = None
            installed = {"path": str(directory / relative), "version": self.catalog["version"], "backend": backend}
            with self.config.lock:
                value = self.config.read()
                atomic_json(self.config.path, {**value, "native_engines": {**value.get("native_engines", {}), backend: installed}})
            self._activate(backend, event)
        except DownloadPaused:
            self._state(backend, status="paused")
        except Exception as exc:
            self._state(backend, status="paused" if event.is_set() else "failed", error="" if event.is_set() else str(exc))
        finally:
            if staging and staging.resolve() != self.root.resolve() and staging.resolve().is_relative_to(self.root.resolve()) and staging.name.startswith(".install-"):
                shutil.rmtree(staging)

    def _activate(self, backend, event):
        self._state(backend, status="waiting")
        while not event.is_set():
            with self.manager.lock:
                if self.manager.stopping.is_set():
                    raise DownloadPaused()
                if not self.manager.current:
                    self.use(backend)
                    self._state(backend, status="done", error="")
                    # Installed files are retained; complete ZIPs no longer need disk space.
                    for archive in self.entries[backend]["archives"]:
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
