"""Run a queue of audio/video files through recognition one at a time.

A batch is a persisted record in the recognition database, not a UI-only list:
closing the window, leaving the page or restarting the service must not turn a
half-finished queue into silence. Each item imports its own private audio copy,
runs the same recogniser used by the single-file page, and — once that item is
done — writes its SRT/TXT next to the original file. One failed file never
stops the queue; it is recorded with its own error and can be retried later.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from ..errors import VoiceError, describe_error
from .exporting import export_transcript
from .formats import ALLOWED_SUFFIXES, VIDEO_SUFFIXES
from .runner import BATCH_IDLE_UNLOAD_MS, DEFAULT_IDLE_UNLOAD_MS

MAX_ITEMS = 200
FORMATS = ("srt", "txt", "srt+txt")
BUSY_STATUSES = ("queued", "running", "cancelling")


class BatchRecognition:
    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.store = recognizer.store
        self.active_id = None
        self.thread = None
        self.cancelled = threading.Event()

    # ------------------------------------------------------------------ read

    def state(self, identity):
        record = self.store.get_batch(identity)
        value = dict(record["state"])
        value["id"] = record["id"]
        value["created"] = record["created"]
        value["updated"] = record["updated"]
        items = value.get("items") or []
        status = value.get("status") or "queued"
        value.update(summary(items, status))
        if status in BUSY_STATUSES:
            # The page polls every second while the queue runs. Each item keeps the
            # counts written when it finished, so polling no longer parses every
            # full transcript in the database.
            for item in items:
                item.setdefault("line_count", 0)
                item.setdefault("translated_lines", 0)
                item.setdefault("translation_target", "")
        else:
            translations = self.store.translation_summary(
                [item.get("session_id") for item in items if item.get("session_id")])
            for item in items:
                info = translations.get(item.get("session_id")) or {}
                item["line_count"] = info.get("line_count", 0)
                item["translated_lines"] = info.get("translated_lines", 0)
                item["translation_target"] = info.get("target", "")
        return value

    def recent(self, limit=30):
        return [self._brief(record) for record in self.store.recent_batches(limit)]

    def active_state(self):
        """Compact progress for the recognition page's poll; no item payload."""
        identity = self.active_id
        if not identity:
            return None
        try:
            return self._brief(self.store.get_batch(identity))
        except VoiceError:
            return None

    def _brief(self, record):
        value = dict(record["state"])
        value["id"] = record["id"]
        value["created"] = record["created"]
        value["updated"] = record["updated"]
        value.update(summary(value.get("items") or [], value.get("status") or "queued"))
        value.pop("items", None)
        return value

    # ----------------------------------------------------------------- start

    def start(self, paths, language="auto", formats="srt", name="", dedupe=True):
        items = prepare(paths)
        value = str(formats or "srt")
        formats = value if value in FORMATS else "srt"
        with self.recognizer.manager.lock:
            if self.active_id:
                raise VoiceError("RECOGNITION_BUSY", "已有批量识别正在进行，请先停止或等待完成", stage="批量识别")
            self.recognizer._require_idle()
            selection = self.recognizer.choose(language=language)
            if not selection.get("asr_model") or not Path(selection["asr_model"]).is_file():
                raise VoiceError("ASR_MODEL_MISSING", "请先在设置 → 模型下载语音识别模型（如 Qwen3-ASR）",
                                 "批量识别需要 ASR 模型；对齐器用于给出精确时间。", "批量识别")
            batch = {
                "name": (name or "").strip()[:200] or f"批量识别 · {len(items)} 个文件",
                "status": "queued", "language": language, "format": formats,
                "dedupe": bool(dedupe),
                "current": None, "error": "", "items": items,
            }
            identity = self.store.create_batch(batch)
            self.active_id = identity
            cancelled = threading.Event()
            self.cancelled = cancelled
            try:
                self._launch(identity, cancelled)
            except Exception:
                self.active_id = None
                self.store.remove_batch(identity)
                raise
        return self.state(identity)

    def retry(self, identity):
        with self.recognizer.manager.lock:
            state = self.state(identity)
            if self.active_id or state.get("status") in BUSY_STATUSES:
                raise VoiceError("RECOGNITION_BUSY", "批量识别正在进行，请先停止或等待完成", stage="批量识别")
            self.recognizer._require_idle()
            pending = []
            for index, item in enumerate(state.get("items") or []):
                if item.get("status") not in ("failed", "cancelled", "pending"):
                    continue
                pending.append(index)
                session_id = item.get("session_id")
                reusable = bool(session_id) and self.recognizer.session_reusable(session_id, identity)
                changes = {"status": "pending", "error": "", "hint": "",
                           "exported": [], "export_error": "", "duration": None, "processing_seconds": None}
                if session_id and not reusable:
                    previous = self._batch_session_state(session_id, identity)
                    if previous and previous.get("status") in ("failed", "cancelled"):
                        try:
                            self.recognizer.remove(session_id)
                        except VoiceError:
                            pass
                    # A "done" session can only appear here after a crash between
                    # recognition and the item update; keep that transcript instead
                    # of deleting a finished result.
                    session_id = None
                if not reusable:
                    changes.update({"line_count": 0, "translated_lines": 0, "translation_target": ""})
                changes["session_id"] = session_id
                self.store.update_batch_item(identity, index, changes)
            if not pending:
                return self.state(identity)
            self.store.update_batch(identity, {"status": "queued", "current": None, "error": ""})
            self.active_id = identity
            cancelled = threading.Event()
            self.cancelled = cancelled
            try:
                self._launch(identity, cancelled)
            except Exception as exc:
                self.active_id = None
                self.store.update_batch(identity, {
                    "status": "failed", "current": None,
                    "error": describe_error(exc, "批量识别")["message"]})
                raise
        return self.state(identity)

    def cancel(self, identity):
        with self.recognizer.manager.lock:
            state = self.state(identity)
            if state.get("status") not in BUSY_STATUSES:
                return state
            self.cancelled.set()
            current = self._current_session(state)
            if current:
                self.recognizer.cancel(current)
            self.store.update_batch(identity, {"status": "cancelling"})
            for index, item in enumerate(state.get("items") or []):
                if item.get("status") in ("pending", "importing"):
                    self.store.update_batch_item(identity, index, {
                        "status": "cancelled", "error": "批量识别已停止，未处理该文件"})
        return self.state(identity)

    def remove(self, identity):
        with self.recognizer.manager.lock:
            if self.active_id == identity:
                raise VoiceError("RECOGNITION_BUSY", "批量识别正在运行，请先停止后再删除", stage="批量识别")
            # Removing only the batch row used to leave every child session (and
            # its PCM copy) behind; the sidebar then showed them as loose records.
            self.recognizer.remove_batch_sessions(identity)
            self.store.remove_batch(identity)
        return {"removed": identity}

    def close(self):
        identity = self.active_id
        if identity:
            try:
                self.cancel(identity)
            except Exception:
                self.cancelled.set()
        else:
            self.cancelled.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=20)
            if thread.is_alive():
                raise RuntimeError("批量识别仍在退出，不能关闭识别数据库")

    # ------------------------------------------------------------------ work

    def _launch(self, identity, cancelled):
        runner = self.recognizer.runner
        runner.idle_unload_ms = BATCH_IDLE_UNLOAD_MS
        thread = threading.Thread(target=self._run, args=(identity, cancelled),
                                  name="recognition-batch", daemon=True)
        self.thread = thread
        try:
            thread.start()
        except Exception:
            runner.idle_unload_ms = DEFAULT_IDLE_UNLOAD_MS
            self.thread = None
            raise

    def _current_session(self, state):
        items = state.get("items") or []
        current = state.get("current")
        if isinstance(current, int) and 0 <= current < len(items):
            return items[current].get("session_id")
        return next((item.get("session_id") for item in items
                     if item.get("status") in ("importing", "running") and item.get("session_id")), None)

    def _batch_session_state(self, session_id, batch_id):
        """State of an old attempt, but only when it belongs to this queue."""
        try:
            state = self.store.get(session_id)["state"]
        except VoiceError:
            return None
        return state if state.get("batch_id") == batch_id else None

    def _run(self, identity, cancelled):
        manager = self.recognizer.manager
        final, failure = "done", ""
        try:
            self.store.update_batch(identity, {"status": "running", "current": None, "error": ""})
            settings = self.store.get_batch(identity)["state"]
            items = settings.get("items") or []
            language = settings.get("language", "auto")
            formats = settings.get("format", "srt")
            dedupe = bool(settings.get("dedupe", True))
            for index in range(len(items)):
                if cancelled.is_set() or manager.stopping.is_set():
                    final = "cancelled"
                    break
                item = self.store.get_batch(identity)["state"]["items"][index]
                if item.get("status") != "pending":
                    continue
                self.store.update_batch(identity, {"current": index})
                self.store.update_batch_item(identity, index, {
                    "status": "importing", "error": "", "hint": "",
                    "exported": [], "export_error": "", "duration": None, "processing_seconds": None})
                self._run_item(identity, index, item, cancelled, language, formats, dedupe)
            if cancelled.is_set() or manager.stopping.is_set():
                final = "cancelled"
        except Exception as exc:
            final = "failed"
            failure = describe_error(exc, "批量识别")["message"]
        finally:
            self._finish(identity, final, failure, cancelled)

    def _run_item(self, identity, index, item, cancelled, language, formats, dedupe=True):
        manager = self.recognizer.manager
        session_id = item.get("session_id")
        try:
            if session_id:
                # Retry reuses the PCM copy imported by the previous attempt.
                self.store.update_batch_item(identity, index, {"status": "running", "session_id": session_id})
                try:
                    self.recognizer.retry(session_id, batch=True, dedupe=dedupe)
                except VoiceError as exc:
                    if exc.code != "RECOGNITION_AUDIO_NOT_FOUND":
                        raise
                    try:
                        self.recognizer.remove(session_id)
                    except VoiceError:
                        pass
                    session_id = None
                    self.store.update_batch_item(identity, index, {"session_id": None})
            if session_id is None:
                session = self.recognizer.import_audio(
                    item["path"], item.get("name") or "", language, batch=True, cancelled=cancelled,
                    batch_id=identity, batch_index=index, dedupe=dedupe)
                session_id = session["id"]
                self.store.update_batch_item(identity, index, {"status": "running", "session_id": session_id})
            state = self._wait(session_id, cancelled)
            if state.get("status") == "done":
                exported, export_error = [], ""
                try:
                    exported = export_transcript(state, formats=formats)
                except Exception as exc:
                    export_error = describe_error(exc, "导出字幕")["message"]
                translation = state.get("translation") or {}
                self.store.update_batch_item(identity, index, {
                    "status": "done", "session_id": session_id, "error": "", "hint": "",
                    "exported": exported, "export_error": export_error,
                    "duration": state.get("duration"), "processing_seconds": state.get("processing_seconds"),
                    "line_count": len(state.get("lines") or []),
                    "translated_lines": sum(1 for line in translation.get("lines") or []
                                            if str(line.get("translated") or "").strip()),
                    "translation_target": translation.get("target") or ""})
                return
            if state.get("status") == "cancelled" or cancelled.is_set() or manager.stopping.is_set():
                self.store.update_batch_item(identity, index, {
                    "status": "cancelled", "session_id": session_id, "error": "批量识别已停止"})
                return
            self.store.update_batch_item(identity, index, {
                "status": "failed", "session_id": session_id,
                "error": state.get("error") or "识别没有完成", "hint": state.get("hint") or ""})
        except Exception as exc:
            error = describe_error(exc, "批量识别")
            status = "cancelled" if cancelled.is_set() or manager.stopping.is_set() else "failed"
            self.store.update_batch_item(identity, index, {
                "status": status, "session_id": session_id, "error": error["message"],
                "hint": error.get("hint", "")})

    def _wait(self, session_id, cancelled):
        manager = self.recognizer.manager
        while True:
            state = self.recognizer.store.get(session_id)["state"]
            if state.get("status") not in ("queued", "running"):
                return state
            if cancelled.is_set() or manager.stopping.is_set():
                self.recognizer.cancel(session_id)
            time.sleep(0.2)

    def _finish(self, identity, final, failure, cancelled):
        manager = self.recognizer.manager
        try:
            if final == "failed":
                current = self._current_session(self.store.get_batch(identity)["state"])
                if current:
                    self.recognizer.cancel(current)
            # One engine served the whole queue; release it before publishing the
            # final status so "done" always means the VRAM has been handed back.
            self.recognizer.release_engine()
        finally:
            try:
                if final == "cancelled":
                    state = self.store.get_batch(identity)["state"]
                    for index, item in enumerate(state.get("items") or []):
                        if item.get("status") in ("pending", "importing"):
                            self.store.update_batch_item(identity, index, {
                                "status": "cancelled", "error": "批量识别已停止，未处理该文件"})
                    self.store.update_batch(identity, {"status": "cancelled", "current": None, "error": ""})
                else:
                    self.store.update_batch(identity, {"status": final, "current": None, "error": failure})
            finally:
                self.active_id = None
                self.thread = None
                manager.wake.set()


def prepare(paths):
    """Validate every selected file before a batch is allowed to exist."""
    values = [Path(str(value)).expanduser() for value in (paths or [])]
    if not values:
        raise VoiceError("RECOGNITION_EMPTY_BATCH", "没有选择要识别的文件", stage="批量识别")
    if len(values) > MAX_ITEMS:
        raise VoiceError("RECOGNITION_BATCH_LIMIT", f"一次最多添加 {MAX_ITEMS} 个文件",
                         "分批添加后逐个批量识别。", "批量识别")
    items, seen = [], set()
    for source in values:
        if not source.is_file():
            raise VoiceError("FILE_MISSING", f"文件不存在：{source}",
                             "重新选择音频或视频文件。", "批量识别")
        suffix = source.suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise VoiceError("RECOGNITION_FORMAT_UNSUPPORTED", f"不支持的文件格式：{source.suffix}",
                             "支持 WAV / FLAC / MP3 / M4A / OGG 等音频，以及 MP4 / MKV / MOV / AVI / WEBM 等视频。",
                             "批量识别")
        key = os.path.normcase(str(source.resolve()))
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "path": str(source.resolve()), "name": source.stem[:200] or "未命名音频",
            "kind": "video" if suffix in VIDEO_SUFFIXES else "audio",
            "status": "pending", "session_id": None, "error": "", "hint": "",
            "exported": [], "export_error": "", "duration": None, "processing_seconds": None})
    return items


def summary(items, status):
    counts = {key: 0 for key in ("pending", "importing", "running", "done", "failed", "cancelled")}
    for item in items:
        key = item.get("status") or "pending"
        counts[key] = counts.get(key, 0) + 1
    current = next((item for item in items if item.get("status") in ("importing", "running")), None)
    return {"status": status, "total": len(items), "current_name": (current or {}).get("name"),
            "done": counts["done"], "failed": counts["failed"], "pending": counts["pending"],
            "running": counts["importing"] + counts["running"], "cancelled": counts["cancelled"],
            "exported": sum(len(item.get("exported") or []) for item in items)}
