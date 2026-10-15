"""Recognition sessions in their own SQLite database, beside their audio copies."""
import json
import sqlite3
import threading
import time
import uuid

from ..errors import VoiceError


class RecognitionStore:
    def __init__(self, root):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "recognition.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL);
            """)
            # An interrupted run has no usable result; the audio copy is kept.
            self.db.execute("UPDATE sessions SET state=json_set(state, '$.status', 'failed', "
                            "'$.error', '上次识别被中断，请重新识别；音频仍保留。') "
                            "WHERE json_extract(state, '$.status') IN ('queued', 'running')")

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
            return [{"id": r["id"], "created": r["created"], "updated": r["updated"],
                     "name": json.loads(r["state"])["name"], "status": json.loads(r["state"]).get("status")}
                    for r in self.db.execute("SELECT * FROM sessions ORDER BY updated DESC LIMIT 200")]

    def remove(self, identity):
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE id=?", (identity,))

    def close(self):
        self.db.close()
