"""Import, versioning and cancellable audio-processing use cases."""
import json
import shutil
import secrets
import threading
import time
import uuid
from pathlib import Path
from ..config import atomic_json
from ..errors import VoiceError
from ..model_info import _read_package
from .pcm import inspect_wave, frame_range, render_wave
from .store import EditStore
from .vevo2 import VevoRunner


class AudioEditor:
    def __init__(self, manager):
        self.manager = manager
        self.root = manager.root / "audio-editor"
        self.root.mkdir(exist_ok=True)
        self.store = EditStore(self.root)
        for version in self.store.unfinished_files():
            # Only known, unfinished version files owned by this project store.
            self.audio_path(version + ".wav")
            for suffix in (".wav", "-source.wav", "-append.wav", "-reference.wav"):
                path = self.root / (version + suffix)
                if path.resolve().parent == self.root.resolve():
                    path.unlink(missing_ok=True)
        self.runner = VevoRunner(self.root)
        self.active = None
        self.thread = None
        self.gpu_busy = False

    def project(self, identity):
        result = self.store.get(identity)
        for version in result["versions"]:
            version["path"] = str(self.audio_path(version["audio"])) if version["audio"] else None
            version["audio_url"] = f"/audio-editor/audio/{version['audio']}" if version["audio"] else None
        return result

    def audio_path(self, filename):
        import re
        if not re.fullmatch(r"[0-9a-f]{32}\.wav", filename or ""):
            raise VoiceError("EDIT_AUDIO_NOT_FOUND", "音频编辑文件不存在")
        path = (self.root / filename).resolve()
        if path.parent != self.root.resolve():
            raise VoiceError("EDIT_AUDIO_NOT_FOUND", "音频文件不在编辑目录中")
        return path

    def import_audio(self, path, name="", original_text=""):
        source = Path(path).resolve()
        inspect_wave(source)
        filename = uuid.uuid4().hex + ".wav"
        target = self.audio_path(filename)
        try:
            shutil.copyfile(source, target)
            details = inspect_wave(target)
            identity = self.store.create({"name": name.strip() or source.stem[:200] or "未命名音频",
                                         "original_text": original_text, "target_text": original_text}, filename, details)
            return self.project(identity)
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def import_model(self, path):
        from ..model_management import path_identity
        path = self.validate_model(path)
        # Local models stay in place; no multi-GB copy or voice preset is made.
        with self.manager.lock, self.manager.config.lock:
            value = self.manager.config.read()
            value.setdefault("audio_edit_models", {})[path_identity(path)] = {"path": str(path), "name": path.stem}
            value["removed_models"] = [p for p in value.get("removed_models", []) if p != path_identity(path)]
            atomic_json(self.manager.config.path, value)
        return self.options()

    @staticmethod
    def validate_model(path):
        try:
            path = Path(path or "").resolve()
            stat = path.stat()
            if not path.is_file() or path.suffix.lower() != ".gguf" or _read_package(str(path), stat.st_size, stat.st_mtime_ns).get("family") != "vevo2":
                raise ValueError("请选择 Vevo2 GGUF 文件")
            return path
        except Exception as exc:
            raise VoiceError("EDIT_MODEL_INVALID", f"无法使用编辑模型：{exc}", stage="音频编辑") from exc

    def options(self):
        from ..model_management import path_identity
        value = self.manager.config.read()
        models = {path_identity(m["path"]): {"path": m["path"], "name": m["name"]}
                  for m in self.manager.models.installed() if m["family"] == "vevo2" and m["exists"]}
        models.update({k: m for k, m in value.get("audio_edit_models", {}).items()
                       if Path(m["path"]).is_file() and k not in value.get("removed_models", [])})
        return {"models": list(models.values()), "runtime": self.manager.models.runtime(),
                "references": self.manager.config.references(), "active": dict(self.active) if self.active else None}

    def submit(self, identity, parameters):
        with self.manager.lock:
            if self.manager.stopping.is_set():
                raise VoiceError("SERVICE_STOPPING", "服务正在停止")
            if self.active:
                raise VoiceError("EDIT_BUSY", "已有音频正在处理，请等待或取消后重试")
            project = self.project(identity)
            source = next((v for v in project["versions"] if v["id"] == parameters["source_version"] and v["status"] == "done"), None)
            if not source:
                raise VoiceError("EDIT_VERSION_INVALID", "请选择本项目已完成的音频版本")
            info = inspect_wave(source["path"])
            first, last = frame_range(info, parameters["start"], parameters["end"])
            # Snap boundaries before persisting the processing snapshot.
            parameters = json.loads(json.dumps(parameters))
            parameters["start"], parameters["end"] = first / info["sample_rate"], last / info["sample_rate"]
            ai = parameters["operation"] in ("vc", "edit")
            if ai:
                self.manager.require_idle_engine("音频编辑")
                if (last - first) / info["sample_rate"] > 60:
                    raise VoiceError("EDIT_SOURCE_TOO_LONG", "第一版 AI 编辑每次最多处理 60 秒，请选择一个短片段")
                self.validate_model(parameters["model_path"])
                runtime = self.manager.models.runtime()
                if not runtime or not Path(runtime).is_file():
                    raise VoiceError("ENGINE_MISSING", "请先在设置中安装或选择 audio.cpp 推理引擎")
                if parameters["operation"] == "edit" and not parameters["state"]["target_text"].strip():
                    raise VoiceError("EMPTY_TEXT", "修改台词时，请填写目标文本")
                reference = parameters.get("reference_path")
                if parameters["operation"] == "vc" and not reference:
                    raise VoiceError("REFERENCE_REQUIRED", "换声需要选择目标音色的参考 WAV")
                if reference:
                    duration = inspect_wave(reference)["duration"]
                    if not 1 <= duration <= 60:
                        raise VoiceError("REFERENCE_DURATION", "目标音色参考需要 1–60 秒的 WAV")
                if parameters["seed"] == -1:
                    parameters["seed"] = secrets.randbits(32)
            elif parameters["operation"] == "append":
                if not parameters.get("append_path"):
                    raise VoiceError("EDIT_APPEND_REQUIRED", "请选择需要拼接的 WAV")
                inspect_wave(parameters["append_path"])
            version = self.store.enqueue(identity, parameters)
            self.active = {"project_id": identity, "version_id": version, "operation": parameters["operation"]}
            self.cancelled = threading.Event()
            self.gpu_busy = ai
            if ai:
                self.manager.release_runners()
            self.thread = threading.Thread(target=self._execute, args=(version, source, parameters, self.cancelled),
                                           name="audio-editor", daemon=True)
            self.thread.start()
        return self.project(identity)

    def _execute(self, version, source, parameters, cancelled):
        output = self.audio_path(version + ".wav")
        staging = self.root / (version + "-source.wav")
        append = self.root / (version + "-append.wav")
        reference = self.root / (version + "-reference.wav")
        started = time.perf_counter()
        outcome = None
        try:
            self.store.finish(version, "running")
            if parameters["operation"] in ("vc", "edit"):
                render_wave(source["path"], staging, start=parameters["start"], end=parameters["end"], cancelled=cancelled)
                if parameters.get("reference_path"):
                    shutil.copyfile(parameters["reference_path"], reference)
                    inspect_wave(reference)
                else:
                    # Editing can preserve the source voice without another reference.
                    render_wave(staging, reference, end=min(10, inspect_wave(staging)["duration"]), cancelled=cancelled)
                model = self.validate_model(parameters["model_path"])
                stat = model.stat()
                voice = {"family": "vevo2", "model_dir": str(model), "engine_executable": self.manager.models.runtime(),
                         "device": parameters["device"], "timeout": 600, "revision": f"{stat.st_size}:{stat.st_mtime_ns}",
                         "task": "vc" if parameters["operation"] == "vc" else "s2s"}
                self.runner.generate_edit(voice, parameters, staging, reference, output, cancelled)
                details = inspect_wave(output)
            else:
                if parameters["operation"] == "append":
                    shutil.copyfile(parameters["append_path"], append)
                details = render_wave(source["path"], output, start=parameters["start"], end=parameters["end"],
                                      gain_db=parameters["gain_db"], fade_in=parameters["fade_in"], fade_out=parameters["fade_out"],
                                      append=append if parameters["operation"] == "append" else None, cancelled=cancelled)
            with self.manager.lock:
                if cancelled.is_set() or self.manager.stopping.is_set():
                    raise VoiceError("CANCELLED", "编辑已取消")
                details["processing_seconds"] = time.perf_counter() - started
                outcome = {"status": "done", "audio": output.name, "details": details}
        except Exception as exc:
            output.unlink(missing_ok=True)
            outcome = {"status": "cancelled" if cancelled.is_set() else "failed", "error": str(exc)}
        finally:
            for path in (staging, append, reference):
                path.unlink(missing_ok=True)
            try:
                # Release Vevo2 after a task so article generation need not share VRAM.
                self.runner.interrupt()
            finally:
                # Clear the flags first and publish the terminal state last, so a version can
                # never look finished while the editor still reports the accelerator busy.
                with self.manager.lock:
                    self.gpu_busy = False
                    self.active = None
                    self.store.finish(version, **outcome)
                    self.manager.wake.set()

    def cancel(self, identity):
        with self.manager.lock:
            if self.active and self.active["project_id"] == identity:
                self.cancelled.set()
                self.runner.interrupt()
        return self.project(identity)

    def close(self):
        if self.active:
            self.cancelled.set()
        self.runner.interrupt()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise RuntimeError("音频编辑仍在退出，不能关闭版本数据库")
        self.store.close()
