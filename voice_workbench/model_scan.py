"""Discover packages declared by the selected engine, without fetching weights."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from .config import atomic_json
from .errors import VoiceError
from .model_updates import repository_files, safe_remote_path

DEFAULT_REPO = "audio-cpp/audio.cpp-gguf"
TASK_NAMES = {"tts": "语音生成", "clone": "声音克隆", "design": "音色设计", "vdes": "音色设计",
              "stt": "语音识别", "asr": "语音识别", "align": "字幕对齐", "vc": "声音转换",
              "music": "音乐生成", "vad": "语音检测", "s2s": "音频处理", "edit": "音频编辑"}


def engine_manifest(runtime):
    """Return a fingerprint and validated family/package declarations."""
    executable = Path(runtime) if runtime else None
    if not executable or not executable.is_file():
        raise ValueError("请先安装或选择推理引擎")
    directory = executable.parent / "model_specs"
    files = sorted(directory.glob("*.json"))[:512]
    if not files:
        raise ValueError("此引擎没有 model_specs 模型声明，请升级引擎后再扫描")
    stat = executable.stat()
    signature = [(str(executable.resolve()), stat.st_size, stat.st_mtime_ns)]
    for path in files:
        stat = path.stat()
        if not path.is_symlink() and stat.st_size <= 2 * 1024**2:
            signature.append((path.name, stat.st_size, stat.st_mtime_ns))
    return _read_manifest(tuple(signature))


@lru_cache(maxsize=8)
def _read_manifest(signature):
    """Polling stats files; unchanged declarations are parsed only once."""
    directory = Path(signature[0][0]).parent / "model_specs"
    families, packages = {}, {}
    for filename, _, _ in signature[1:]:
        try:
            path = directory / filename
            spec = json.loads(path.read_text(encoding="utf-8"))
            family = spec.get("family", "")
            if not re.fullmatch(r"[a-z0-9_]{1,80}", family):
                continue
            tasks = [t for t in spec.get("tasks", []) if isinstance(t, str)]
            status = spec.get("status", "unknown")
            families[family] = {"family": family, "name": str(spec.get("display_name") or family)[:200],
                                "tasks": tasks, "status": status}
            if status in ("wip", "planned", "unsupported", "disabled"):
                continue
            defaults = spec.get("package_defaults", {}).get("download", {})
            for package in spec.get("packages", []):
                if package.get("format") != "gguf":
                    continue
                download = {**defaults, **package.get("download", {})}
                repo, revision = download.get("repo", DEFAULT_REPO), download.get("revision", "main")
                if (download.get("gated") or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or
                        not re.fullmatch(r"[\w.-]{1,100}", revision)):
                    continue
                paths = package.get("files", [])
                # A package may require sidecars or several GGUF files. Show it,
                # but never feed an incomplete package to the single-file adapter.
                ggufs = [p for p in paths if safe_remote_path(p) and p.lower().endswith(".gguf")]
                for remote in ggufs:
                    name = str(package.get("display_name") or spec.get("display_name") or family)[:200]
                    if len(ggufs) > 1:
                        name += " · " + Path(remote).name
                    packages[(repo, revision, remote)] = {
                        "family": family, "name": name,
                        "precision": str(package.get("precision", "")).upper().replace("Q8_0", "Q8"),
                        "tasks": tasks, "engine_status": status, "repo": repo, "remotePath": remote,
                        "package_files": paths, "single_file": len(paths) == 1,
                        "version": "2.5" if family == "index_tts2" and "2.5" in remote else "2.0" if family == "index_tts2" else "",
                        "variant": "voicedesign" if "voicedesign" in remote.lower() else "customvoice" if "customvoice" in remote.lower() else "base"}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if not families:
        raise ValueError("引擎的模型声明无法读取")
    key = hashlib.sha256(json.dumps(signature, ensure_ascii=True).encode()).hexdigest()
    return key, list(families.values()), packages


class ModelScanner:
    def __init__(self, models):
        self.models = models
        self.path = models.root / "scan.json"
        self.thread = None
        self.cancel = threading.Event()
        self.key = None
        self.attempted = None
        self.checked = None
        self.families = []
        self.error = ""
        self.warnings = []
        self.scanning = False
        self.discovered_ids = set()
        self.cached_entries = []

    def state(self):
        return {"scanning": self.scanning, "checked": self.checked, "families": self.families,
                "error": self.error, "warnings": self.warnings}

    def ensure(self):
        """Called on model-page refresh; one automatic attempt per engine/session."""
        if self.scanning or self.models.closed:
            return
        try:
            key, families, packages = engine_manifest(self.models.runtime())
        except (OSError, ValueError):
            return
        if key == self.attempted:
            return
        if key != self.key:
            self._restore(key, families, packages)
        if self.checked and key == self.key and time.time() - self.checked < 86400:
            self.attempted = key
            return
        self.start(manifest=(key, families, packages), automatic=True)

    def _restore(self, key, families, packages):
        try:
            cached = json.loads(self.path.read_text(encoding="utf-8"))
            if cached.get("engine_key") != key:
                return
            declarations = {(p["repo"], p["remotePath"], p["family"]) for p in packages.values()}
            entries = []
            for entry in cached.get("entries", []):
                if ((entry.get("repo"), entry.get("remotePath"), entry.get("family")) not in declarations or
                        not re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", "")) or
                        not re.fullmatch(r"[a-f0-9]{40}", entry.get("revision", "")) or
                        type(entry.get("size")) is not int or not 0 < entry["size"] < 32 * 1024**3):
                    continue
                # Rebuild all labels, compatibility and IDs from this engine;
                # only immutable remote metadata is trusted from the cache.
                declaration = next(p for p in packages.values() if (p["repo"], p["remotePath"], p["family"]) ==
                                   (entry["repo"], entry["remotePath"], entry["family"]))
                entries.append(self._entry(declaration, entry))
            if entries and isinstance(cached.get("checked"), (int, float)):
                warnings = [w[:1000] for w in cached.get("warnings", []) if isinstance(w, str)][:50]
                self._apply(key, families, entries, cached["checked"], warnings)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _entry(self, declaration, metadata):
        from .model_management import FAMILIES
        original = next((e for e in self.models.base_catalog.values() if
                         (e.get("repo") or DEFAULT_REPO, e.get("remotePath"), e.get("family")) ==
                         (declaration["repo"], declaration["remotePath"], declaration["family"])), {})
        identity = original.get("id") or "scan-" + hashlib.sha256(
            (declaration["family"] + ":" + declaration["repo"] + ":" + declaration["remotePath"]).encode()).hexdigest()[:20]
        return {**declaration, **original, **{k: metadata[k] for k in ("size", "sha256", "revision")},
                "id": identity, "repo": declaration["repo"], "discovered": True,
                "adapted": declaration["family"] in FAMILIES and declaration["single_file"]}

    def start(self, source="mirror", manifest=None, automatic=False):
        if source not in ("mirror", "huggingface", "modelscope"):
            raise VoiceError("MODEL_SCAN_SOURCE_INVALID", "请选择有效的模型来源", stage="模型扫描")
        try:
            key, families, packages = manifest or engine_manifest(self.models.runtime())
        except (OSError, ValueError) as exc:
            raise VoiceError("MODEL_SCAN_UNAVAILABLE", str(exc), stage="模型扫描") from exc
        with self.models.lock:
            if self.models.closed:
                raise VoiceError("SERVICE_STOPPING", "服务正在停止", stage="模型扫描")
            if self.scanning:
                return self.state()
            if self.models.checking_updates or any(t.is_alive() for t in self.models.threads.values()):
                if automatic:
                    return self.state()
                raise VoiceError("DOWNLOAD_BUSY", "请先完成或暂停模型下载、更新检查再扫描", stage="模型扫描")
            self.scanning, self.attempted = True, key
            self.error, self.warnings, self.families = "", [], families
            self.thread = threading.Thread(target=self._scan, args=(key, families, packages, source), name="model-scan", daemon=True)
            self.thread.start()
        return self.state()

    def _scan(self, key, families, packages, source):
        entries, warnings = [], []
        def fetch(repo_revision):
            if self.cancel.is_set():
                return repo_revision, None, ""
            try:
                with self.models.client_factory() as client:
                    revision, files = repository_files(client, repo_revision[0], source, repo_revision[1])
                return repo_revision, (revision, files), ""
            except Exception as exc:
                return repo_revision, None, f"{repo_revision[0]}：{exc}"
        try:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="model-metadata") as pool:
                results = pool.map(fetch, sorted({k[:2] for k in packages}))
                for (repo, requested_revision), result, error in results:
                    if self.cancel.is_set():
                        return
                    if error:
                        warnings.append(error)
                    if result:
                        revision, files = result
                        for (r, rev, remote), declaration in packages.items():
                            if (r, rev) == (repo, requested_revision) and remote in files:
                                entries.append(self._entry(declaration, {**files[remote], "revision": revision}))
            if not entries:
                raise ValueError("没有获取到声明中的 GGUF 文件及校验值。" + (warnings[0] if warnings else ""))
            # An engine can change while the network is in flight. Discard stale
            # results instead of attaching the old capabilities to the new one.
            if engine_manifest(self.models.runtime())[0] != key or self.cancel.is_set():
                return
            with self.models.lock:
                if self.models.closed:
                    return
                checked = time.time()
                retained = {e["id"]: e for e in self.cached_entries} if key == self.key else {}
                retained.update({e["id"]: e for e in entries})
                entries = list(retained.values())
                atomic_json(self.path, {"engine_key": key, "checked": checked, "entries": entries, "warnings": warnings})
                self._apply(key, families, entries, checked, warnings)
        except Exception as exc:
            with self.models.lock:
                self.error = "扫描失败，已保留原有目录：" + str(exc)
        finally:
            with self.models.lock:
                self.scanning = False

    def _apply(self, key, families, entries, checked, warnings):
        with self.models.lock:
            # A partial repository failure must not erase last-known packages.
            old = {i: self.models.catalog[i] for i in self.discovered_ids if i in self.models.catalog} if key == self.key else {}
            discovered = {**old, **{e["id"]: e for e in entries if e["id"] not in self.models.base_catalog}}
            self.models.catalog = {**self.models.base_catalog, **discovered}
            for entry in entries:
                if entry["id"] in self.models.base_catalog:
                    self.models.updates[entry["id"]] = entry
            self.discovered_ids = set(discovered)
            self.cached_entries = entries
            self.key, self.families, self.checked, self.warnings = key, families, checked, warnings

    def close(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise RuntimeError("模型扫描仍在退出中")
