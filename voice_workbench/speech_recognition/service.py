"""Import audio, run transcription plus alignment, and keep the result per session."""
from __future__ import annotations

import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..config import atomic_json
from ..errors import VoiceError
from . import segments
from .audio_import import import_wave
from .lines import (build_lines, estimate_units, extract_units, fallback_lines,
                    reattach_punctuation)
from .runner import RecognitionRunner
from .languages import ALIGN_LANGUAGES, alignment_language
from .store import RecognitionStore

# Formats the native reader and the Qt player both accept.
AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
# Video containers the bundled Qt/FFmpeg decoder opens; only the audio track is kept.
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts", ".m2ts",
                  ".mts", ".mpg", ".mpeg", ".3gp", ".ogv"}
ALLOWED_SUFFIXES = AUDIO_SUFFIXES | VIDEO_SUFFIXES

# A clip that needs this many windows is recognised window by window rather than in
# one request. A single request over a very long file silently drops speech — measured
# on a 27-minute track, one request produced 6.7 characters per second where the same
# audio transcribed window by window produced 10.8 — and a transcript that is missing
# content can never be matched to the audio again, which is what left whole windows
# showing text that was spoken a minute earlier.
PIECEWISE_RECOGNITION_WINDOWS = 3

# The aligner emits one 80 ms frame for a word it could not place. Used to tell a
# real short word from a placeholder.
ONE_FRAME_SECONDS = 0.085

# How far a placeholder end may be stretched to reach the next word. Without a cap a
# single word absorbed the 30 seconds of silence that followed it and the subtitle
# sat on screen for half a minute.
MAX_WORD_SECONDS = 2.5


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
            raise VoiceError("FILE_MISSING", f"文件不存在：{source}", "重新选择音频或视频文件。", "导入音频")
        suffix = source.suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise VoiceError("RECOGNITION_FORMAT_UNSUPPORTED", f"不支持的音频/视频格式：{source.suffix}",
                             "支持 WAV / FLAC / MP3 / M4A / OGG 等音频，以及 MP4 / MKV / MOV / AVI / WEBM 等视频"
                             "（只取音轨识别，原文件不变）。", "导入音频")
        video = suffix in VIDEO_SUFFIXES
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
            details = import_wave(source, target, cancelled=self.manager.stopping, video=video)
            with self.manager.lock:
                self._require_idle()
                identity = self.store.create({
                    "name": (name or "").strip() or source.stem[:200] or "未命名音频",
                    "audio": filename, "source_path": str(source), "source_kind": "video" if video else "audio",
                    "language": language,
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

            requested = state.get("language", "auto")
            windows = segments.windows(audio, state.get("duration"))
            transcript, units, warning, text, language = {}, [], "", "", ""
            if align_voice and len(windows) >= PIECEWISE_RECOGNITION_WINDOWS:
                # Long clip: recognise and align on the same windows, so no text has
                # to be dealt out by assumed speech rate.
                text, units, warning, language, wall_ms = recognize_windows(
                    self.runner, voices, audio, windows, requested, cancelled)
                transcript = {"timing": {"wall_ms": round(wall_ms, 1)}}
                if not text:
                    warning = "未识别到文字，可能是静音或没有清晰语音；请检查音频后重试。"
            else:
                transcript = self.runner.transcribe(voices, audio, requested, cancelled)
                text = str(transcript.get("text") or "").strip()
                language = alignment_language(requested, transcript.get("language"))
                if not text:
                    warning = "未识别到文字，可能是静音或没有清晰语音；请检查音频后重试。"
                elif align_voice:
                    if not language:
                        warning = "已识别文字，但引擎未返回语言，已按标点估算时间；请选择音频语言后重新识别以获得精确时间。"
                    elif language not in ALIGN_LANGUAGES:
                        warning = f"已识别文字，但对齐器不支持语言 {language}，已按标点估算每行时间。"
                    else:
                        try:
                            units, warning = align_units(self.runner, voices, audio, text, language,
                                                        state.get("duration"), cancelled)
                            if not units:
                                warning = "对齐器没有返回时间点，已按标点估算每行时间；建议改用更短的音频重试。"
                        except Exception as exc:
                            if cancelled.is_set() or isinstance(exc, VoiceError) and exc.code == "CANCELLED":
                                raise
                            warning = "文字识别已完成，时间对齐失败，已按标点估算每行时间：" + str(exc)[:500]
                else:
                    warning = "未安装强制对齐器，每行时间按标点估算；安装 Qwen3 对齐器后可精确到词。"
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "识别已取消")
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


def align_units(runner, voices, audio, text, language, duration, cancelled):
    """Align a transcript, in pieces when the clip is too long for one request.

    Returns ``(units, warning)``. A long clip is cut at quiet points and each
    piece is aligned with its own share of the text, because one request for the
    whole file either exceeds the engine's body cap or comes back as segment-level
    units. A piece whose alignment fails still gets times, distributed inside its
    own span, so a single bad request can no longer cost the transcript its
    timing the way it used to.
    """
    pieces = segments.plan(audio, text, duration)
    if not pieces:
        return extract_units(runner.align(voices, audio, text, language, cancelled)), ""
    if not str(tempfile.gettempdir()).isascii():
        raise VoiceError("NATIVE_PATH_ENCODING", "系统临时目录含非 ASCII 字符",
                         "设置 TMP 为可写的英文路径后重启程序。", "字幕对齐")
    units, failed, note = [], [], ""
    with tempfile.TemporaryDirectory(prefix="shengjian-alignment-") as folder:
        for index, piece in enumerate(pieces, start=1):
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "识别已取消")
            staged = Path(folder) / f"piece-{index:04d}.wav"
            local = []
            try:
                segments.cut(audio, piece["start"], piece["end"], staged)
                local = extract_units(runner.align(voices, staged, piece["text"], language, cancelled))
            except Exception as exc:
                if cancelled.is_set() or isinstance(exc, VoiceError) and exc.code == "CANCELLED":
                    raise
                note = note or str(exc)[:300]
            finally:
                staged.unlink(missing_ok=True)
            if local and _looks_aligned(local, piece["end"] - piece["start"]):
                units.extend((word, start + piece["start"], end + piece["start"])
                             for word, start, end in _fill_short_ends(local, piece["end"] - piece["start"]))
            else:
                failed.append(index)
                note = note or "对齐器返回的时间点不可信（每个词只有一帧）"
                units.extend(estimate_units(piece["text"], piece["start"], piece["end"]))
    if not failed:
        return units, ""
    return units, (f"{len(failed)}/{len(pieces)} 个片段未能精确对齐，这些片段按其时间范围估算："
                   + (note or "对齐器没有返回时间点"))


def _looks_aligned(units, span):
    """Is a returned timing plausible, or did the aligner give up?

    A request that cannot match its text answers one 80 ms frame per word and leaves
    the rest of the window to the last word; the starts then bunch into a fraction of
    the window. That is how a single line came to span 17 seconds around the word
    "jea". A real alignment spreads its starts across the audio it matched, however
    much of the window that happens to be, so a cluster at the front — not a low
    coverage on its own — is the signal to estimate this window instead.
    """
    if len(units) < 4:
        return True
    first, last = units[0][1], units[-1][2]
    covered = last - first
    if covered < span * 0.1:
        return False
    spans = sorted(end - start for _, start, end in units)
    median = spans[len(spans) // 2]
    quarter = first + covered * 0.25
    early = sum(1 for _, start, _ in units if start <= quarter)
    # Bunched at the front *and* every word exactly one frame: the aligner gave up on
    # the start of the window and only found the later words.
    return not (early >= len(units) * 0.55 and median <= ONE_FRAME_SECONDS)


def _fill_short_ends(units, span):
    """A word reported as one frame ends where the next word starts.

    The aligner emits ``end = start + 1 frame`` for words it located but did not
    measure, so a line built from those ends lasted 80 ms and the following line
    started seconds later. Filling them makes the end of a line agree with the start
    of the next one; durations that were actually measured are left alone.
    """
    filled = []
    for index, (word, start, end) in enumerate(units):
        following = units[index + 1][1] if index + 1 < len(units) else None
        if end - start <= ONE_FRAME_SECONDS and following is not None and following > end:
            end = min(following, start + MAX_WORD_SECONDS)
        filled.append((word, start, end))
    if len(filled) >= 2 and filled[-1][2] - filled[-1][1] <= ONE_FRAME_SECONDS:
        typical = (filled[-1][1] - filled[0][1]) / max(1, len(filled) - 1)
        word, start, _ = filled[-1]
        filled[-1] = (word, start, min(span, start + min(MAX_WORD_SECONDS, max(0.1, typical))))
    return filled


def recognize_windows(runner, voices, audio, windows, requested_language, cancelled):
    """Transcribe and align each window with its own text.

    Returns ``(text, units, warning, language, wall_ms)``. Recognising the file as a
    whole loses speech on long recordings, and the leftover transcript can only be
    dealt out by assumed speech rate — the windows then hold text that belongs
    elsewhere, which the aligner answers with a frame per word. Recognising where the
    words actually are removes the guess: every window is aligned against exactly what
    was said in it, so a window can fail on its own without dragging the rest along.

    The transcript is the concatenation of the window texts, joined with a space where
    the script separates words. A word repeated across a window boundary is dropped
    once, since cutting at a quiet point does not guarantee a clean word edge.
    """
    if not str(tempfile.gettempdir()).isascii():
        raise VoiceError("NATIVE_PATH_ENCODING", "系统临时目录含非 ASCII 字符",
                         "设置 TMP 为可写的英文路径后重启程序。", "语音识别")
    units, failed, language = [], [], ""
    started = time.perf_counter()
    # "auto" makes every window guess for itself, and a window that guesses wrong is
    # transcribed worse. The first window decides, the rest inherit it.
    voice = requested_language
    with tempfile.TemporaryDirectory(prefix="shengjian-recognition-") as folder:
        for index, window in enumerate(windows, start=1):
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "识别已取消")
            staged = Path(folder) / f"piece-{index:04d}.wav"
            body, detected, local = "", "", []
            try:
                segments.cut(audio, window["start"], window["end"], staged)
                payload = runner.transcribe(voices, staged, voice, cancelled)
                body = str(payload.get("text") or "").strip()
                detected = alignment_language(requested_language, payload.get("language")) or ""
                # The alignment reads the staged file, so it has to happen before the
                # cleanup below deletes it.
                if body and detected:
                    local = extract_units(runner.align(voices, staged, body, detected, cancelled))
            except Exception as exc:
                if cancelled.is_set() or isinstance(exc, VoiceError) and exc.code == "CANCELLED":
                    raise
                failed.append(index)
                continue
            finally:
                staged.unlink(missing_ok=True)
            if not body:
                continue
            language = language or detected
            if detected and str(voice).lower() in ("auto", "none", ""):
                voice = detected
            span = window["end"] - window["start"]
            if local and _looks_aligned(local, span):
                local = _fill_short_ends(reattach_punctuation(local, body), span)
                local = [(word, start + window["start"], end + window["start"])
                         for word, start, end in local]
            else:
                local = estimate_units(body, window["start"], window["end"])
                failed.append(index)
            if units and _first_word(local[0][0]) and _first_word(local[0][0]) == _last_word(units[-1][0]):
                # Same word on both sides of the cut: it was spoken once.
                local = local[1:]
                if not local:
                    continue
            units.extend(local)
            if units and not units[-1][0].endswith((" ", "\n")):
                word, start, end = units[-1]
                units[-1] = (word + (" " if " " in body else ""), start, end)
    text = "".join(word for word, _, _ in units).strip()
    wall_ms = (time.perf_counter() - started) * 1000
    if failed:
        note = f"{len(failed)}/{len(windows)} 个片段未能精确对齐，这些片段按其时间范围估算"
        return text, units, note, language, wall_ms
    return text, units, "", language, wall_ms


def _first_word(text):
    return _edge_word(text)


def _last_word(text):
    return _edge_word(text, last=True)


def _edge_word(text, last=False):
    """First or last word of a unit, normalised, for spotting a duplicated seam word."""
    tokens = [token for token in re.split(r"[^0-9A-Za-z']+", text or "") if token]
    if not tokens:
        return ""
    return (tokens[-1] if last else tokens[0]).lower()


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
