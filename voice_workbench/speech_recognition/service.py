"""Import audio, run transcription plus alignment, and keep the result per session."""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from ..config import atomic_json
from ..errors import VoiceError
from .audio_import import import_wave
from .lines import build_lines, extract_units, fallback_lines
from .runner import RecognitionRunner
from .languages import ALIGN_LANGUAGES, alignment_language
from .store import RecognitionStore

# Formats the native reader and the Qt player both accept.
ALLOWED_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}


class SpeechRecognizer:
    def __init__(self, manager):
        self.manager = manager
        self.root = manager.root / "recognition"
        self.root.mkdir(exist_ok=True)
        self.store = RecognitionStore(self.root)
        self.runner = RecognitionRunner(self.root)
        self.active = None
        self.thread = None
        self.gpu_busy = False

    # ---------------------------------------------------------------- discovery

    def _available(self):
        models = self.manager.models.installed()
        by_task = {"asr": [], "align": []}
        for model in models:
            tasks = model.get("tasks") or []
            for task in ("asr", "align"):
                if task in tasks:
                    by_task[task].append({"path": model["path"], "name": model["name"],
                                          "family": model["family"], "precision": model.get("precision", "")})
        return by_task

    # ------------------------------------------------------------------ config

    def _selection(self):
        value = self.manager.config.read().get("recognition", {})
        return value if isinstance(value, dict) else {}

    def choose(self, asr_model=None, align_model=None, language=None):
        available = self._available()
        with self.manager.config.lock:
            value = self.manager.config.read()
            current = value.get("recognition", {}) if isinstance(value.get("recognition"), dict) else {}
            if asr_model is not None:
                current["asr_model"] = asr_model
            if align_model is not None:
                current["align_model"] = align_model
            if language is not None:
                current["language"] = language
            known = {m["path"] for group in available.values() for m in group}
            for key in ("asr_model", "align_model"):
                if current.get(key) and not Path(current[key]).is_file():
                    current.pop(key)
            if current.get("asr_model") not in known and available["asr"]:
                current["asr_model"] = available["asr"][0]["path"]
            if current.get("align_model") not in known and available["align"]:
                current["align_model"] = available["align"][0]["path"]
            value["recognition"] = current
            atomic_json(self.manager.config.path, value)
        return current

    def options(self):
        available = self._available()
        return {"asr": available["asr"], "align": available["align"], "runtime": self.manager.models.runtime(),
                "selection": self._selection(), "languages": LANGUAGE_CHOICES,
                "active": dict(self.active) if self.active else None}

    # ------------------------------------------------------------------ session

    def session(self, identity):
        record = self.store.get(identity)
        state = record["state"]
        state["audio_url"] = f"/recognition/audio/{state['audio']}" if state.get("audio") else None
        return {**record, "state": state}

    def audio_path(self, filename):
        import re
        if not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,5}", filename or ""):
            raise VoiceError("RECOGNITION_AUDIO_NOT_FOUND", "识别音频不存在", stage="语音识别")
        path = (self.root / filename).resolve()
        if path.parent != self.root.resolve():
            raise VoiceError("RECOGNITION_AUDIO_NOT_FOUND", "音频不在识别目录中", stage="语音识别")
        return path

    def import_audio(self, path, name="", language="auto"):
        source = Path(path).resolve()
        if not source.is_file():
            raise VoiceError("FILE_MISSING", f"音频不存在：{source}", "重新选择音频文件。", "导入音频")
        if source.suffix.lower() not in ALLOWED_SUFFIXES:
            raise VoiceError("RECOGNITION_FORMAT_UNSUPPORTED", f"不支持的音频格式：{source.suffix}",
                             "支持 WAV / FLAC / MP3 / M4A / OGG 等常见格式。", "导入音频")
        with self.manager.lock:
            self._require_idle()
        selection = self.choose(language=language)
        # Refuse before copying anything: a session without an ASR model could only fail.
        if not selection.get("asr_model") or not Path(selection["asr_model"]).is_file():
            raise VoiceError("ASR_MODEL_MISSING", "请先在设置 → 模型下载语音识别模型（如 Qwen3-ASR）",
                             "识别需要 ASR 模型；对齐器用于给出精确时间。", "语音识别")
        filename = uuid.uuid4().hex + ".wav"
        target = self.audio_path(filename)
        try:
            # Decoding must not hold the queue lock. Recheck admission before committing.
            details = import_wave(source, target, cancelled=self.manager.stopping)
            with self.manager.lock:
                self._require_idle()
                identity = self.store.create({
                    "name": (name or "").strip() or source.stem[:200] or "未命名音频",
                    "audio": filename, "source_path": str(source), "language": language,
                    "duration": details["duration"], "status": "queued", "error": None,
                    "text": "", "lines": [], "processing_seconds": None,
                    "asr_model": selection.get("asr_model"), "align_model": selection.get("align_model")})
                try:
                    self._launch(identity)
                except Exception:
                    self.store.remove(identity)
                    raise
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.session(identity)

    def retry(self, identity):
        with self.manager.lock:
            state = self.store.get(identity)["state"]
            self._require_idle()
            if not self.audio_path(state["audio"]).is_file():
                raise VoiceError("RECOGNITION_AUDIO_NOT_FOUND", "音频文件已不存在，请重新导入", stage="语音识别")
            selection = self.choose()
            self._launch(identity, {"asr_model": selection.get("asr_model"), "align_model": selection.get("align_model")})
        return self.session(identity)

    def remove(self, identity):
        with self.manager.lock:
            state = self.store.get(identity)["state"]
            if self.active and self.active["session_id"] == identity:
                raise VoiceError("RECOGNITION_BUSY", "正在识别这条音频，请先取消", stage="语音识别")
            self.store.remove(identity)
            if state.get("audio"):
                self.audio_path(state["audio"]).unlink(missing_ok=True)
        return {"removed": identity}

    def _require_idle(self):
        if self.active:
            raise VoiceError("RECOGNITION_BUSY", "已有音频正在识别，请等待或取消后重试", stage="语音识别")
        self.manager.require_idle_engine("语音识别")

    def _launch(self, identity, changes=None):
        with self.manager.lock:
            self._require_idle()
            previous = self.store.get(identity)["state"]
            self.store.update(identity, {**(changes or {}), "status": "queued", "error": None})
            try:
                self.active = {"session_id": identity}
                self.cancelled = threading.Event()
                self.gpu_busy = True
                self.manager.release_runners()
                thread = threading.Thread(target=self._execute, args=(identity, self.cancelled),
                                          name="recognition", daemon=True)
                thread.start()
                self.thread = thread
            except Exception:
                self.active, self.gpu_busy = None, False
                self.store.update(identity, previous)
                self.manager.wake.set()
                raise

    def _execute(self, identity, cancelled):
        started = time.perf_counter()
        outcome = None
        try:
            state = self.store.get(identity)["state"]
            self.store.update(identity, {"status": "running"})
            asr_path, align_path = state.get("asr_model"), state.get("align_model")
            if not asr_path or not Path(asr_path).is_file():
                raise VoiceError("ASR_MODEL_MISSING", "请先在设置 → 模型下载语音识别模型（如 Qwen3-ASR）",
                                 "识别需要 ASR 模型；对齐器用于给出精确时间。", "语音识别")
            runtime = self.manager.models.runtime()
            if not runtime or not Path(runtime).is_file():
                raise VoiceError("ENGINE_MISSING", "请先在设置中安装或选择 audio.cpp 推理引擎", stage="语音识别")
            device = self.manager.config.read().get("native_engine_backend") or "cuda"
            audio = self.audio_path(state["audio"])
            voices = [{"family": "qwen3_asr", "model_dir": asr_path, "engine_executable": runtime,
                       "device": device, "timeout": 900, "task": "asr"}]
            voices[0]["family"] = state.get("asr_family") or _family_of(asr_path, "qwen3_asr")
            align_voice = None
            if align_path and Path(align_path).is_file():
                align_voice = {"family": _family_of(align_path, "qwen3_forced_aligner"), "model_dir": align_path,
                               "engine_executable": runtime, "device": device, "timeout": 900, "task": "align"}
            if align_voice:
                voices.append(align_voice)

            transcript = self.runner.transcribe(voices, audio, state.get("language", "auto"), cancelled)
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "识别已取消")
            text = str(transcript.get("text") or "").strip()
            language = alignment_language(state.get("language"), transcript.get("language"))
            warning = ""
            units = []
            if not text:
                warning = "未识别到文字，可能是静音或没有清晰语音；请检查音频后重试。"
            elif align_voice:
                if not language:
                    warning = "已识别文字，但引擎未返回语言，已按标点估算时间；请选择音频语言后重新识别以获得精确时间。"
                elif language not in ALIGN_LANGUAGES:
                    warning = f"已识别文字，但对齐器不支持语言 {language}，已按标点估算每行时间。"
                else:
                    try:
                        payload = self.runner.align(voices, audio, text, language, cancelled)
                        units = extract_units(payload)
                        if not units:
                            warning = "对齐器没有返回时间点，已按标点估算每行时间；建议改用更短的音频重试。"
                    except Exception as exc:
                        if cancelled.is_set() or isinstance(exc, VoiceError) and exc.code == "CANCELLED":
                            raise
                        warning = "文字识别已完成，时间对齐失败，已按标点估算每行时间：" + str(exc)[:500]
            else:
                warning = "未安装强制对齐器，每行时间按标点估算；安装 Qwen3 对齐器后可精确到词。"
            lines = (build_lines(units, state.get("duration"), text=text) if units
                     else fallback_lines(text, state.get("duration")))
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "识别已取消")
            outcome = {"status": "done", "error": None, "text": text, "lines": lines,
                       "warning": warning, "processing_seconds": time.perf_counter() - started,
                       "recognized_language": language,
                       "timing": transcript.get("timing")}
        except Exception as exc:
            from ..errors import describe_error
            if cancelled.is_set():
                outcome = {"status": "cancelled", "error": "识别已取消"}
            else:
                error = describe_error(exc, "语音识别")
                outcome = {"status": "failed", "error": error["message"], "hint": error.get("hint", ""),
                           "processing_seconds": time.perf_counter() - started}
        finally:
            try:
                # Release the engine so article generation need not share VRAM.
                self.runner.interrupt()
            finally:
                # Clear the flags first and publish the terminal state last, so a session can
                # never look finished while the software still reports the engine busy.
                with self.manager.lock:
                    self.gpu_busy = False
                    self.active = None
                    self.store.update(identity, outcome)
                    self.manager.wake.set()

    def cancel(self, identity):
        with self.manager.lock:
            if self.active and self.active["session_id"] == identity:
                self.cancelled.set()
                self.runner.interrupt()
            elif self.store.get(identity)["state"].get("status") in ("queued", "running"):
                self.store.update(identity, {"status": "cancelled", "error": "识别已取消"})
        return self.session(identity)

    def close(self):
        if self.active:
            self.cancelled.set()
        self.runner.interrupt()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise RuntimeError("语音识别仍在退出，不能关闭识别数据库")
        self.store.close()


def _family_of(path, fallback):
    try:
        from ..model_info import _read_package
        stat = Path(path).stat()
        return _read_package(str(path), stat.st_size, stat.st_mtime_ns).get("family") or fallback
    except Exception:
        return fallback


LANGUAGE_CHOICES = [("自动判断", "auto"), ("中文", "zh"), ("英语", "en"), ("粤语", "yue"), ("日语", "ja"),
                    ("韩语", "ko"), ("德语", "de"), ("法语", "fr"), ("西班牙语", "es"), ("俄语", "ru"),
                    ("葡萄牙语", "pt"), ("意大利语", "it")]
