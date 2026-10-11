"""Model catalog adapted from yovoice (Apache-2.0); weights stay under their licenses."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import threading

import httpx

from .config import atomic_json
from .errors import VoiceError
from .model_info import model_ui
from .schemas import VoiceConfig

REVISION = "6d5436fc85f7a20c2e9f4e472b7f3a532f686444"
SOURCES = {"huggingface": "https://huggingface.co", "mirror": "https://hf-mirror.com", "modelscope": "https://modelscope.cn"}
FAMILIES = {"index_tts2", "voxcpm2", "kokoro_tts", "qwen3_tts"}


def model_description(entry):
    family = entry.get("family")
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


class DownloadPaused(Exception):
    pass


class ModelManager:
    def __init__(self, manager, catalog=None, client_factory=None):
        self.manager, self.config = manager, manager.config
        self.root = manager.root / "models"
        self.root.mkdir(exist_ok=True)
        entries = catalog if catalog is not None else json.loads((Path(__file__).parent / "assets" / "model_catalog.json").read_text(encoding="utf-8"))
        self.catalog = {v["id"]: v for v in entries if v.get("family") in FAMILIES}
        self.lock = threading.RLock()
        self.downloads = {}
        self.threads = {}
        self.cancel = {}
        self.closed = False
        self.client_factory = client_factory or (lambda: httpx.Client(follow_redirects=True, timeout=httpx.Timeout(10, connect=5, read=5)))

    def runtime(self):
        value = self.config.read()
        configured = value.get("native_engine") or ""
        candidates = [configured] + [v.get("engine_executable", "") for v in self.config.voices() if v.get("backend") == "audio_cpp"]
        for path in candidates:
            if path and Path(path).is_file():
                return path
        return configured

    def set_runtime(self, path):
        if not Path(path).is_file():
            raise VoiceError("ENGINE_MISSING", "请选择存在的 audiocpp_server.exe", stage="模型管理")
        with self.manager.lock, self.config.lock:
            if self.manager.current:
                raise VoiceError("ENGINE_BUSY", "正在生成音频，请完成或取消后更换引擎", stage="模型管理")
            self.manager.release_runners()
            value = self.config.read()
            for group in ("voices", "model_templates"):
                for voice in value.get(group, []):
                    if voice.get("backend") == "audio_cpp":
                        voice["engine_executable"] = str(Path(path).resolve())
            value["native_engine"] = str(Path(path).resolve())
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
            entry = next((e for e in self.catalog.values() if Path(e.get("remotePath", "")).name == path.name and e.get("remotePath") and e["family"] == voice.get("family")), {})
            try:
                size = path.stat().st_size if path.is_file() else 0
            except OSError:
                size = 0
            models[identity] = {"id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                "name": entry.get("name", path.stem), "precision": entry.get("precision", ""), "family": voice.get("family"),
                "path": str(path), "size": size, "is_directory": path.is_dir(),
                "exists": path.exists(), "managed": path.parent == self.root.resolve(),
                "can_delete_file": voice["backend"] == "audio_cpp" and path.suffix.lower() == ".gguf",
                "preset_count": int(not template), "catalog_id": entry.get("id"),
                "description": model_description(entry or voice)}
        return list(models.values())

    def listing(self):
        installed = self.installed()
        present = {m["catalog_id"] for m in installed if m["exists"]}
        with self.lock:
            available = []
            for identity, entry in self.catalog.items():
                part = self.root / (Path(entry.get("remotePath") or identity + ".gguf").name + ".part")
                state = self.downloads.get(identity, {"status": "paused" if part.exists() else "available", "received": part.stat().st_size if part.exists() else 0})
                if state["status"] == "done" and identity not in present:
                    state = {"status": "available", "received": 0}
                available.append({**entry, "description": model_description(entry), **state,
                                  "installed": identity in present, "downloadable": bool(entry.get("remotePath"))})
        return {"installed": installed, "catalog": available, "runtime": self.runtime()}

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
        if actual not in FAMILIES or (family and family != actual):
            raise VoiceError("MODEL_FAMILY_MISMATCH", "模型内嵌配置不属于支持的语音模型家族", stage="模型导入")
        voice = VoiceConfig(id="imported-model", name="默认模型设置", backend="audio_cpp", python=sys.executable,
                            model_dir=str(path), engine_executable=self.runtime(), family=actual).model_dump()
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
            if self.manager.current or queued:
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
            value["removed_models"] = sorted(set(value.get("removed_models", [])) | {target})
            atomic_json(self.config.path, value)
            return {"removed": identity, "file_deleted": delete_file}

    def start_download(self, identity, source="mirror"):
        entry = self.catalog.get(identity)
        if entry is None:
            raise VoiceError("MODEL_NOT_FOUND", "模型不在下载目录中", stage="模型下载")
        url = download_url(entry, source)
        with self.lock:
            if self.closed:
                raise VoiceError("SERVICE_STOPPING", "服务正在停止", stage="模型下载")
            if any(t.is_alive() for t in self.threads.values()):
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
        if path.stat().st_size != entry["size"]:
            raise ValueError(f"下载文件大小不符：应为 {entry['size']} 字节，实际 {path.stat().st_size} 字节")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if event.is_set():
                    raise DownloadPaused()
                digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            if path.suffix == ".part":
                path.unlink(missing_ok=True)
            raise ValueError("SHA-256 校验失败，请重新下载或移除不匹配的本地模型文件")

    def _download(self, entry, url, event):
        identity = entry["id"]
        target = self.root / Path(entry["remotePath"]).name
        part = target.with_name(target.name + ".part")
        try:
            if not target.is_file():
                offset = part.stat().st_size if part.exists() else 0
                if offset > entry["size"]:
                    part.unlink()
                    offset = 0
                self._state(identity, received=offset)
                if offset < entry["size"]:
                    with self.client_factory() as client:
                        with client.stream("GET", url, headers={"Range": f"bytes={offset}-"} if offset else {}, follow_redirects=True) as response:
                            response.raise_for_status()
                            if response.status_code == 206:
                                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
                                if not match or int(match[1]) != offset or int(match[3]) != entry["size"]:
                                    raise ValueError("下载服务器返回的续传范围不正确，请更换下载源")
                            elif response.status_code == 200:
                                offset = 0  # A server may ignore Range; never append its entire response.
                            else:
                                raise ValueError(f"下载服务器返回 HTTP {response.status_code}")
                            if response.headers.get("content-length") and int(response.headers["content-length"]) != entry["size"] - offset:
                                raise ValueError("下载服务器返回的文件大小与模型目录不一致")
                            self._state(identity, resumed_from=offset)
                            with part.open("ab" if offset else "wb") as stream:
                                for chunk in response.iter_bytes():
                                    if event.is_set():
                                        raise DownloadPaused()
                                    if offset + len(chunk) > entry["size"]:
                                        raise ValueError("下载数据超过模型文件大小")
                                    stream.write(chunk)
                                    offset += len(chunk)
                                    self._state(identity, received=offset)
                self._state(identity, status="verifying")
                self._verify(part, entry, event)
                if event.is_set():
                    raise DownloadPaused()
                os.replace(part, target)
            else:
                self._state(identity, status="verifying")
                self._verify(target, entry, event)
            self.register(target, entry["family"])
            self._state(identity, status="done", received=entry["size"], error="")
        except DownloadPaused:
            self._state(identity, status="paused")
        except Exception as exc:
            self._state(identity, status="paused" if event.is_set() else "failed", error="" if event.is_set() else str(exc))

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancel.values():
                event.set()
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=20)
            if thread.is_alive():
                raise RuntimeError("模型下载仍在退出中")
