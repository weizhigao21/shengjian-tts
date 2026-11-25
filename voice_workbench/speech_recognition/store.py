"""Recognition sessions in their own SQLite database, beside their audio copies."""
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from ..errors import VoiceError

AUDIO_NAME = re.compile(r"[0-9a-f]{32}\.[a-z0-9]{1,5}")


class RecognitionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "recognition.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL);
            """)
            # An interrupted run has no usable result; the audio copy is kept.
            self.db.execute("UPDATE sessions SET state=json_set(state, '$.status', 'failed', "
                            "'$.error', '上次识别被中断，请重新识别；音频仍保留。') "
                            "WHERE json_extract(state, '$.status') IN ('queued', 'running')")
            self._recover_batches()
        self.prune_audio()

    def create(self, state):
        identity = uuid.uuid4().hex
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                            (identity, now, now, json.dumps(state, ensure_ascii=False)))
        return identity

    def get(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("RECOGNITION_NOT_FOUND", "识别记录不存在", stage="语音识别")
            return {"id": row["id"], "created": row["created"], "updated": row["updated"],
                    "state": json.loads(row["state"])}

    def update(self, identity, changes):
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM sessions WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("RECOGNITION_NOT_FOUND", "识别记录不存在", stage="语音识别")
            state = {**json.loads(row["state"]), **changes}
            self.db.execute("UPDATE sessions SET state=?, updated=? WHERE id=?",
                            (json.dumps(state, ensure_ascii=False), time.time(), identity))
        return state

    def recent(self):
        with self.lock:
            result = []
            for row in self.db.execute("SELECT * FROM sessions ORDER BY updated DESC LIMIT 200"):
                state = json.loads(row["state"])
                translation = state.get("translation") or {}
                lines = state.get("lines") or []
                translated = translation.get("lines") or []
                translated_lines = sum(1 for line in translated
                                      if str(line.get("translated") or "").strip())
                result.append({
                    "id": row["id"], "created": row["created"], "updated": row["updated"],
                    "name": state.get("name"), "status": state.get("status"),
                    "batch_id": state.get("batch_id"), "batch_index": state.get("batch_index"),
                    "line_count": len(lines), "translated_lines": translated_lines,
                    "target": translation.get("target") or "",
                })
            return result

    def translation_summary(self, identities):
        """Per-session line counts without loading every full transcript."""
        values = [str(value) for value in identities if value]
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        result = {}
        with self.lock:
            rows = self.db.execute(f"SELECT id,state FROM sessions WHERE id IN ({placeholders})",
                                   values).fetchall()
        for row in rows:
            state = json.loads(row["state"])
            lines = state.get("lines") or []
            translation = state.get("translation") or {}
            translated = translation.get("lines") or []
            result[row["id"]] = {
                "line_count": len(lines),
                "translated_lines": sum(1 for line in translated
                                        if str(line.get("translated") or "").strip()),
                "target": translation.get("target") or "",
            }
        return result

    def remove(self, identity):
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE id=?", (identity,))

    def prune_audio(self):
        """Delete WAV copies no session references (left by a crash or an old retry)."""
        with self.lock:
            rows = self.db.execute("SELECT json_extract(state,'$.audio') AS audio FROM sessions").fetchall()
        referenced = {row["audio"] for row in rows if row["audio"]}
        removed = 0
        for path in self.root.glob("*.wav"):
            if path.name in referenced or not AUDIO_NAME.fullmatch(path.name):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    def batch_sessions(self, identity):
        with self.lock:
            rows = self.db.execute("SELECT id,state FROM sessions "
                                   "WHERE json_extract(state,'$.batch_id')=?", (identity,)).fetchall()
        return [(row["id"], json.loads(row["state"])) for row in rows]

    def remove_sessions(self, identities):
        values = [str(value) for value in identities if value]
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        with self.lock, self.db:
            cursor = self.db.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", values)
        return cursor.rowcount

    # ----------------------------------------------------------------- batches

    def _session_audio_file(self, identity):
        row = self.db.execute("SELECT json_extract(state,'$.audio') AS audio "
                              "FROM sessions WHERE id=?", (identity,)).fetchone()
        if row is None or not row["audio"]:
            return False
        name = row["audio"]
        return bool(AUDIO_NAME.fullmatch(name)) and (self.root / name).is_file()

    def _recover_batches(self):
        """A queue that was running when the service stopped can be retried."""
        rows = self.db.execute("SELECT id,state FROM batches WHERE json_extract(state,'$.status') "
                               "IN ('queued','running','cancelling')").fetchall()
        for row in rows:
            state = json.loads(row["state"])
            for item in state.get("items") or []:
                if item.get("status") in ("pending", "importing", "running"):
                    item["status"] = "pending"
                    item["error"] = ""
                    item["hint"] = ""
                    # Keep the old session only when its audio copy survived; retry
                    # can then reuse it instead of decoding the source again.
                    if not (item.get("session_id") and self._session_audio_file(item["session_id"])):
                        item["session_id"] = None
                    item["exported"] = []
                    item["export_error"] = ""
            state["status"] = "interrupted"
            state["current"] = None
            state["error"] = "上次批量识别被中断，可点「重试未完成」继续。"
            self.db.execute("UPDATE batches SET state=?, updated=? WHERE id=?",
                            (json.dumps(state, ensure_ascii=False), time.time(), row["id"]))

    def create_batch(self, state):
        identity = uuid.uuid4().hex
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO batches VALUES (?,?,?,?)",
                            (identity, now, now, json.dumps(state, ensure_ascii=False)))
        return identity

    def get_batch(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM batches WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("RECOGNITION_BATCH_NOT_FOUND", "批量识别记录不存在", stage="批量识别")
            return {"id": row["id"], "created": row["created"], "updated": row["updated"],
                    "state": json.loads(row["state"])}

    def update_batch(self, identity, changes):
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM batches WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("RECOGNITION_BATCH_NOT_FOUND", "批量识别记录不存在", stage="批量识别")
            state = {**json.loads(row["state"]), **changes}
            self.db.execute("UPDATE batches SET state=?, updated=? WHERE id=?",
                            (json.dumps(state, ensure_ascii=False), time.time(), identity))
        return state

    def update_batch_item(self, identity, index, changes):
        """Read-modify-write one item under a single lock.

        The queue thread and a cancel request touch the same item; two separate
        get/update calls could drop one of the changes.
        """
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM batches WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("RECOGNITION_BATCH_NOT_FOUND", "批量识别记录不存在", stage="批量识别")
            state = json.loads(row["state"])
            items = state.get("items") or []
            if not 0 <= index < len(items):
                raise VoiceError("RECOGNITION_BATCH_NOT_FOUND", "批量识别记录中的文件不存在", stage="批量识别")
            items[index] = {**items[index], **changes}
            state["items"] = items
            self.db.execute("UPDATE batches SET state=?, updated=? WHERE id=?",
                            (json.dumps(state, ensure_ascii=False), time.time(), identity))
        return state

    def recent_batches(self, limit=30):
        with self.lock:
            return [{"id": row["id"], "created": row["created"], "updated": row["updated"],
                     "state": json.loads(row["state"])}
                    for row in self.db.execute("SELECT * FROM batches ORDER BY updated DESC LIMIT ?", (limit,))]

    def remove_batch(self, identity):
        with self.lock, self.db:
            self.db.execute("DELETE FROM batches WHERE id=?", (identity,))

    def close(self):
        self.db.close()
