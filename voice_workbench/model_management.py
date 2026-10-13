"""Model catalog adapted from yovoice (Apache-2.0); weights stay under their licenses."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import struct
import sys
import threading
import time

import httpx

from .config import atomic_json
from .errors import VoiceError
from .downloads import DownloadPaused, download_file, verify_file
from .model_info import model_ui
from .model_updates import fetch_model_updates
from .model_scan import ModelScanner, TASK_NAMES
from .model_dates import installed_publication, publication_metadata, retain_publication
from .schemas import VoiceConfig

REVISION = "6d5436fc85f7a20c2e9f4e472b7f3a532f686444"
SOURCES = {"huggingface": "https://huggingface.co", "mirror": "https://hf-mirror.com", "modelscope": "https://modelscope.cn"}
FAMILIES = {"index_tts2", "voxcpm2", "kokoro_tts", "qwen3_tts"}


def model_description(entry):
    family = entry.get("family")
    if family == "vevo2":
        return "音频编辑模式：换声、修改台词；支持中文和英语，使用参考 WAV。"
    if family == "kokoro_tts":
        return "轻量语音模型，使用内置音色；不支持声音克隆或文本设计音色。"
    if family == "index_tts2":
        if entry.get("version", entry.get("index_version")) == "2.5":
            return "参考音频克隆、情绪和语速控制；支持中文、英语等语言。"
        return "中文和英语声音克隆，支持情绪控制。"
    if family == "voxcpm2":
        return "多语言声音克隆、带原文的精细克隆和文本设计音色。"
    if family == "qwen3_tts":
        variant = entry.get("variant", "base")
        return {"base": "参考音频克隆；0.6B 版本体积更小。", "customvoice": "使用模型自带的音色，支持风格指令。",
                "voicedesign": "按文字描述设计音色，无需参考音频。"}.get(variant, "多语言语音生成，功能取决于模型版本。")
    if entry.get("discovered"):
        tasks = "、".join(dict.fromkeys(TASK_NAMES.get(t, t) for t in entry.get("tasks", [])))
        return (tasks or "音频模型") + "；引擎已声明，声笺待适配。"
    return "使用独立 Python 环境的本地模型。"


def download_url(entry, source):
    if source not in SOURCES or not entry.get("remotePath"):
        raise VoiceError("MODEL_DOWNLOAD_UNAVAILABLE", "该模型仅支持导入本地文件，或下载源无效", stage="模型下载")
    repo = entry.get("repo") or "audio-cpp/audio.cpp-gguf"
    revision = entry.get("revision") or REVISION
    if source == "modelscope" and not entry.get("repo"):
        return f"https://modelscope.cn/models/HereIsMark/audio.cpp-gguf/resolve/master/{entry['remotePath']}"
    host = SOURCES["mirror"] if source == "modelscope" else SOURCES[source]
    return f"{host}/{repo}/resolve/{revision}/{entry['remotePath']}"


def path_identity(path):
    return os.path.normcase(str(Path(path).resolve()))


class ModelManager:
    def __init__(self, manager, catalog=None, client_factory=None):
        self.manager, self.config = manager, manager.config
        self.root = manager.root / "models"
        self.root.mkdir(exist_ok=True)
        entries = catalog if catalog is not None else json.loads((Path(__file__).parent / "assets" / "model_catalog.json").read_text(encoding="utf-8"))
        self.catalog = {v["id"]: v for v in entries if v.get("family") in FAMILIES}
        self.base_catalog = dict(self.catalog)
        self.updates = {}
        self.update_checked = None
        self.update_error = ""
        self.checking_updates = False
        self.update_path = self.root / "updates.json"
        try:
            cached = json.loads(self.update_path.read_text(encoding="utf-8"))
            for identity, entry in cached.get("entries", {}).items():
                original = self.catalog.get(identity, {})
                if original and all(entry.get(k) == original.get(k) for k in ("id", "family", "remotePath", "repo")):
                    if re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", "")) and re.fullmatch(r"[a-f0-9]{40}", entry.get("revision", "")) and 0 < entry.get("size", 0) < 32 * 1024**3:
                        self.updates[identity] = entry
            self.update_checked = cached.get("checked")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.lock = threading.RLock()
        self.downloads = {}
        self.threads = {}
        self.cancel = {}
        self.closed = False
        self.client_factory = client_factory or (lambda: httpx.Client(follow_redirects=True, timeout=httpx.Timeout(10, connect=5, read=5)))
        from .engine_management import EngineManager
        self.engines = EngineManager(self)
        self.scanner = ModelScanner(self)

    def check_updates(self, source="mirror"):
        with self.lock:
            if self.checking_updates or self.scanner.scanning or any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("DOWNLOAD_BUSY", "请先完成或暂停模型下载再检查更新", stage="模型更新")
            self.checking_updates = True
        try:
            candidates = fetch_model_updates({i: e for i, e in self.catalog.items() if e.get("adapted", e["family"] in FAMILIES)}, self.client_factory, source)
            with self.lock:
                self.updates = {i: retain_publication(e, self.updates.get(i, {})) for i, e in candidates.items()}
                self.update_checked = time.time()
                self.update_error = ""
                atomic_json(self.update_path, {"checked": self.update_checked, "entries": self.updates})
        except VoiceError as exc:
            self.update_error = str(exc)
            raise
        finally:
            self.checking_updates = False
        return self.listing()

    def runtime(self):
        value = self.config.read()
        configured = value.get("native_engine") or ""
        candidates = [configured] + [v.get("engine_executable", "") for v in self.config.voices() if v.get("backend") == "audio_cpp"]
        for path in candidates:
            if path and Path(path).is_file():
                return path
        return configured

    def set_runtime(self, path, backend=None, version=None):
        if not Path(path).is_file():
            raise VoiceError("ENGINE_MISSING", "请选择存在的 audiocpp_server.exe", stage="模型管理")
        with self.manager.lock, self.config.lock:
            if self.manager.current or getattr(self.manager, "editing_busy", False):
                raise VoiceError("ENGINE_BUSY", "正在生成音频，请完成或取消后更换引擎", stage="模型管理")
            self.manager.release_runners()
            value = self.config.read()
            for group in ("voices", "model_templates"):
                for voice in value.get(group, []):
                    if voice.get("backend") == "audio_cpp":
                        voice["engine_executable"] = str(Path(path).resolve())
                        if backend:
                            voice["device"] = backend
            value["native_engine"] = str(Path(path).resolve())
            value["native_engine_backend"] = backend
            value["native_engine_version"] = version
            atomic_json(self.config.path, value)
        return {"runtime": self.runtime()}

    def installed(self):
        models = {}
        value = self.config.read()
        for voice, template in [(v, False) for v in value["voices"]] + [(v, True) for v in value.get("model_templates", [])]:
            if voice["backend"] == "windows_sapi" or not voice.get("model_dir"):
                continue
            path = Path(voice["model_dir"]).resolve()
            identity = path_identity(path)
            if identity in models:
                if not template:
                    models[identity]["preset_count"] += 1
                continue
            file_version = value.get("model_file_versions", {}).get(identity, {})
            entry = self.catalog.get(file_version.get("catalog_id"), {}) or next((e for e in self.catalog.values() if Path(e.get("remotePath", "")).name == path.name and e.get("remotePath") and e["family"] == voice.get("family")), {})
            update = self.updates.get(entry.get("id"), {})
            update_available = bool(update and update["sha256"] != file_version.get("sha256", entry.get("sha256")))
            state = self.downloads.get(entry.get("id"), {}) if self.downloads.get(entry.get("id"), {}).get("update") else {}
            try:
                size = path.stat().st_size if path.is_file() else 0
            except OSError:
                size = 0
            models[identity] = {"id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                "name": entry.get("name", path.stem), "precision": entry.get("precision", ""), "family": voice.get("family"),
                "path": str(path), "size": size, "is_directory": path.is_dir(),
                "exists": path.exists(), "managed": path.is_relative_to(self.root.resolve()),
                "can_delete_file": voice["backend"] == "audio_cpp" and path.suffix.lower() == ".gguf",
                "preset_count": int(not template), "catalog_id": entry.get("id"),
                "description": model_description(entry or voice), "update_available": update_available,
                **installed_publication(update or entry, file_version),
                "update_status": state.get("status"), "update_received": state.get("received", 0),
                "update_error": state.get("error", ""), "update_size": update.get("size", 0),
                "revision": file_version.get("revision", entry.get("revision") or REVISION) if entry else "",
                "new_revision": update.get("revision", ""), "can_rollback": bool(file_version.get("previous_path") and Path(file_version["previous_path"]).is_file())}
        # Engine-only packages are verified and stored without creating a voice
        # preset or sending unsupported parameters through a generation adapter.
        for identity, metadata in value.get("model_file_versions", {}).items():
            entry = self.catalog.get(metadata.get("catalog_id"), {}) or metadata.get("entry", {})
            path = Path(identity)
            if (identity in models or identity in value.get("removed_models", []) or not entry.get("discovered") or entry.get("adapted") or
                    not path.is_relative_to(self.root.resolve()) or not path.is_file()):
                continue
            models[identity] = {"id": hashlib.sha256(identity.encode()).hexdigest()[:24], "name": entry["name"],
                "precision": entry["precision"], "family": entry["family"], "path": str(path), "size": path.stat().st_size,
                "is_directory": False, "exists": True, "managed": True, "can_delete_file": True, "preset_count": 0,
                "catalog_id": entry["id"], "description": model_description(entry), "adapted": False,
                "update_available": False, "update_status": None, "revision": metadata.get("revision", ""),
                "can_rollback": False}
            models[identity].update(installed_publication(entry, metadata))
        for identity, entry in value.get("audio_edit_models", {}).items():
            path = Path(entry["path"])
            if identity not in models and path.is_file() and identity not in value.get("removed_models", []):
                models[identity] = {"id": hashlib.sha256(identity.encode()).hexdigest()[:24], "name": entry["name"],
                    "family": "vevo2", "precision": "本地", "path": str(path), "size": path.stat().st_size,
                    "is_directory": False, "exists": True, "managed": False, "can_delete_file": False,
                    "preset_count": 0, "catalog_id": None, "description": model_description({"family": "vevo2"}),
                    "adapted": False, "update_available": False, "update_status": None, "revision": "", "can_rollback": False}
        for entry in models.values():
            entry["edit_adapted"] = entry["family"] == "vevo2"
        return list(models.values())

    def listing(self):
        self.scanner.ensure()
        installed = self.installed()
        present = {m["catalog_id"] for m in installed if m["exists"]}
        with self.lock:
            available = []
            for identity, entry in self.catalog.items():
                part = self.download_target(entry).with_suffix(".gguf.part") if entry.get("remotePath") else self.root / (identity + ".gguf.part")
                state = self.downloads.get(identity, {"status": "paused" if part.exists() else "available", "received": part.stat().st_size if part.exists() else 0})
                if state["status"] == "done" and identity not in present:
                    state = {"status": "available", "received": 0}
                effective = self.updates.get(identity, entry)
                adapted = effective.get("adapted", effective.get("family") in FAMILIES)
                available.append({**effective, **publication_metadata(effective), "description": model_description(effective), **state,
                                  "installed": identity in present, "adapted": adapted,
                                  "edit_adapted": effective["family"] == "vevo2" and effective.get("single_file", True),
                                  "downloadable": bool(entry.get("remotePath")) and effective.get("single_file", True)})
        return {"installed": installed, "catalog": available, "runtime": self.runtime(), "engine": self.engines.listing(),
                "update_checked": self.update_checked, "update_error": self.update_error,
                "checking_updates": self.checking_updates, "scan": self.scanner.state()}

    def download_target(self, entry):
        filename = Path(entry["remotePath"]).name
        # Discovered packages can share a basename across repositories/folders.
        if entry["id"] not in self.base_catalog:
            return self.root / "discovered" / entry["id"] / filename
        if entry["sha256"] != self.base_catalog[entry["id"]]["sha256"]:
            return self.root / "versions" / entry["sha256"][:16] / filename
        return self.root / filename

    def register(self, path, family=None):
        path = Path(path).resolve()
        if not path.is_file() or path.suffix.lower() != ".gguf":
            raise VoiceError("MODEL_MISSING", "请选择存在的 GGUF 模型文件", stage="模型导入")
        from .model_info import _read_package
        stat = path.stat()
        try:
            actual = _read_package(str(path), stat.st_size, stat.st_mtime_ns).get("family")
        except (OSError, ValueError, TypeError, KeyError, struct.error) as exc:
            raise VoiceError("MODEL_METADATA_INVALID", f"无法读取模型配置：{exc}", stage="模型导入") from exc
        if actual == "vevo2" and family in (None, "vevo2"):
            self.manager.audio_editor.import_model(path)
            return {"family": "vevo2", "path": str(path), "edit_adapted": True}
        if actual not in FAMILIES or (family and family != actual):
            raise VoiceError("MODEL_FAMILY_MISMATCH", "模型内嵌配置不属于支持的语音模型家族", stage="模型导入")
        voice = VoiceConfig(id="imported-model", name="默认模型设置", backend="audio_cpp", python=sys.executable,
                            model_dir=str(path), engine_executable=self.runtime(), family=actual,
                            device=self.config.read().get("native_engine_backend") or "cuda").model_dump()
        info = model_ui(voice)
        voice["voice_mode"] = info["modes"][0]
        if info["builtin_voices"]:
            voice["native_voice"] = "zf_xiaobei" if "zf_xiaobei" in info["builtin_voices"] else info["builtin_voices"][0]
        if info["languages"] and voice["language"] not in info["languages"]:
            voice["language"] = info["languages"][0]
        with self.manager.lock, self.config.lock:
            if self.manager.stopping.is_set():
                raise VoiceError("SERVICE_STOPPING", "服务正在停止，模型文件已保留，重启后可重新添加", stage="模型导入")
            value = self.config.read()
            self.config.keep_model(value, voice)
            value["removed_models"] = [p for p in value.get("removed_models", []) if p != path_identity(path)]
            atomic_json(self.config.path, value)
        return next(m for m in self.installed() if path_identity(m["path"]) == path_identity(path))

    def remove(self, identity, delete_file=False):
        with self.manager.lock, self.config.lock:
            model = next((m for m in self.installed() if m["id"] == identity), None)
            if not model:
                raise VoiceError("MODEL_NOT_FOUND", "模型已移除，请刷新列表", stage="模型删除")
            target = path_identity(model["path"])
            queued = any(s["status"] in ("queued", "running") and path_identity(s["voice"].get("model_dir") or ".") == target
                         for j in self.manager.store.list(10000) for s in self.manager.store.get(j["id"])["segments"])
            if self.manager.current or getattr(self.manager, "editing_busy", False) or queued:
                raise VoiceError("ENGINE_BUSY", "模型正在使用或有排队任务，请完成或取消后删除", stage="模型删除")
            with self.lock:
                downloading = any(t.is_alive() and Path(self.catalog[key].get("remotePath", "")).name == Path(model["path"]).name
                                  for key, t in self.threads.items())
            if downloading:
                raise VoiceError("DOWNLOAD_BUSY", "此模型正在下载或校验，请先暂停并等待完成", stage="模型删除")
            if delete_file and not model["can_delete_file"]:
                raise VoiceError("MODEL_DELETE_UNSUPPORTED", "目录模型只支持移出程序；请自行管理其文件", stage="模型删除")
            self.manager.release_runners()
            if delete_file:
                try:
                    Path(model["path"]).unlink(missing_ok=True)
                except OSError as exc:
                    raise VoiceError("MODEL_DELETE_FAILED", f"无法删除模型文件：{exc}", "关闭占用此模型的程序后重试。", "模型删除") from exc
            value = self.config.read()
            for group in ("voices", "model_templates"):
                value[group] = [v for v in value.get(group, []) if not v.get("model_dir") or path_identity(v["model_dir"]) != target]
            value.get("audio_edit_models", {}).pop(target, None)
            value["removed_models"] = sorted(set(value.get("removed_models", [])) | {target})
            if delete_file:
                value.get("model_file_versions", {}).pop(target, None)
            atomic_json(self.config.path, value)
            return {"removed": identity, "file_deleted": delete_file}

    def start_download(self, identity, source="mirror"):
        entry = self.updates.get(identity) or self.catalog.get(identity)
        if entry is None:
            raise VoiceError("MODEL_NOT_FOUND", "模型不在下载目录中", stage="模型下载")
        if not entry.get("single_file", True):
            raise VoiceError("MODEL_PACKAGE_UNAVAILABLE", "此模型需要多个配套文件，暂不支持自动安装", stage="模型下载")
        url = download_url(entry, source)
        with self.lock:
            if self.closed:
                raise VoiceError("SERVICE_STOPPING", "服务正在停止", stage="模型下载")
            if self.checking_updates or self.scanner.scanning or any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("DOWNLOAD_BUSY", "已有模型正在下载，请等待或暂停后再下载其他模型", stage="模型下载")
            event = threading.Event()
            self.cancel[identity] = event
            self.downloads[identity] = {"status": "downloading", "received": 0, "source": source, "error": ""}
            thread = threading.Thread(target=self._download, args=(entry, url, event), name="model-download", daemon=True)
            self.threads[identity] = thread
            thread.start()
            return dict(self.downloads[identity])

    def pause(self, identity):
        with self.lock:
            if identity in self.cancel:
                self.cancel[identity].set()
                if self.threads[identity].is_alive():
                    self.downloads[identity]["status"] = "pausing"
        return {"paused": identity}

    def _state(self, identity, **changes):
        with self.lock:
            self.downloads[identity].update(changes)

    def _verify(self, path, entry, event):
        verify_file(path, entry, event)

    def _download(self, entry, url, event):
        identity = entry["id"]
        target = self.download_target(entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            download_file(target, entry, url, event, self.client_factory, lambda **changes: self._state(identity, **changes))
            if entry.get("adapted", entry["family"] in FAMILIES):
                self.register(target, entry["family"])
            else:
                from .model_info import _read_package
                stat = target.stat()
                if _read_package(str(target), stat.st_size, stat.st_mtime_ns).get("family") != entry["family"]:
                    raise ValueError("模型内嵌家族与引擎声明不一致")
            self._remember_version(target, entry)
            self._state(identity, status="done", received=entry["size"], error="")
        except DownloadPaused:
            self._state(identity, status="paused")
        except Exception as exc:
            self._state(identity, status="paused" if event.is_set() else "failed", error="" if event.is_set() else str(exc))

    def _remember_version(self, path, entry, previous=None):
        with self.config.lock:
            value = self.config.read()
            value.setdefault("model_file_versions", {})[path_identity(path)] = {
                "catalog_id": entry["id"], "sha256": entry["sha256"], "revision": entry.get("revision") or REVISION,
                **publication_metadata(entry),
                "previous_path": previous or ""}
            if not entry.get("adapted", entry["family"] in FAMILIES):
                value["model_file_versions"][path_identity(path)]["entry"] = entry
            value["removed_models"] = [p for p in value.get("removed_models", []) if p != path_identity(path)]
            atomic_json(self.config.path, value)

    def start_update(self, identity, source="mirror"):
        model = next((m for m in self.installed() if m["id"] == identity), None)
        if not model or not model.get("catalog_id") or not model.get("update_available"):
            raise VoiceError("MODEL_UPDATE_UNAVAILABLE", "请先检查更新；此模型没有可用的新版本", stage="模型更新")
        entry = dict(self.updates[model["catalog_id"]])
        url = download_url(entry, source)
        with self.lock:
            if self.closed or self.manager.stopping.is_set():
                raise VoiceError("SERVICE_STOPPING", "服务正在停止", stage="模型更新")
            if self.checking_updates or self.scanner.scanning or any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("DOWNLOAD_BUSY", "已有模型正在下载，请等待或暂停", stage="模型更新")
            event = threading.Event()
            self.cancel[entry["id"]] = event
            self.downloads[entry["id"]] = {"status": "downloading", "received": 0, "source": source, "error": "", "update": True}
            thread = threading.Thread(target=self._update, args=(model, entry, url, event), name="model-update", daemon=True)
            self.threads[entry["id"]] = thread
            thread.start()
            return dict(self.downloads[entry["id"]])

    def _update(self, model, entry, url, event):
        identity = entry["id"]
        target = self.root / "versions" / entry["sha256"][:16] / Path(entry["remotePath"]).name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            download_file(target, entry, url, event, self.client_factory, lambda **changes: self._state(identity, **changes))
            from .model_info import _read_package, validate_capabilities
            stat = target.stat()
            actual = _read_package(str(target), stat.st_size, stat.st_mtime_ns).get("family")
            if actual != model["family"]:
                raise ValueError("新版模型的家族与原模型不一致，原模型已保留")
            self._state(identity, status="waiting")
            while not event.is_set():
                with self.manager.lock, self.config.lock:
                    if self.manager.stopping.is_set():
                        raise DownloadPaused()
                    if not self.manager.current and not getattr(self.manager, "editing_busy", False):
                        value = self.config.read()
                        for group in ("voices", "model_templates"):
                            for voice in value.get(group, []):
                                if path_identity(voice.get("model_dir") or ".") == path_identity(model["path"]):
                                    candidate = {**voice, "model_dir": str(target)}
                                    validate_capabilities(candidate)
                                    voice["model_dir"] = str(target)
                                    voice["revision"] = entry["revision"][:12]
                        self.manager.release_runners()
                        value.setdefault("model_file_versions", {})[path_identity(target)] = {
                            "catalog_id": identity, "sha256": entry["sha256"], "revision": entry["revision"], "previous_path": model["path"],
                            **publication_metadata(entry)}
                        atomic_json(self.config.path, value)
                        self._state(identity, status="done", received=entry["size"], error="", update=False)
                        return
                event.wait(.25)
            raise DownloadPaused()
        except DownloadPaused:
            self._state(identity, status="paused")
        except Exception as exc:
            self._state(identity, status="paused" if event.is_set() else "failed", error="" if event.is_set() else str(exc))

    def rollback(self, identity):
        with self.manager.lock, self.config.lock:
            if self.manager.current or getattr(self.manager, "editing_busy", False) or any(t.is_alive() for t in self.threads.values()):
                raise VoiceError("ENGINE_BUSY", "请先完成生成或模型下载再回退", stage="模型回退")
            model = next((m for m in self.installed() if m["id"] == identity), None)
            value = self.config.read()
            metadata = value.get("model_file_versions", {}).get(path_identity(model["path"]), {}) if model else {}
            previous = metadata.get("previous_path")
            if not previous or not Path(previous).is_file():
                raise VoiceError("MODEL_ROLLBACK_MISSING", "没有可用的旧模型文件", stage="模型回退")
            from .model_info import validate_capabilities
            for group in ("voices", "model_templates"):
                for voice in value.get(group, []):
                    if path_identity(voice.get("model_dir") or ".") == path_identity(model["path"]):
                        candidate = {**voice, "model_dir": previous}
                        validate_capabilities(candidate)
                        voice["model_dir"] = previous
                        voice["revision"] = value.get("model_file_versions", {}).get(path_identity(previous), {}).get("revision", REVISION)[:12]
            self.manager.release_runners()
            atomic_json(self.config.path, value)
        return self.listing()

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancel.values():
                event.set()
            threads = list(self.threads.values())
        self.scanner.close()
        self.engines.close()
        for thread in threads:
            thread.join(timeout=20)
            if thread.is_alive():
                raise RuntimeError("模型下载仍在退出中")
