"""Immutable audio versions in their own SQLite database."""
import json
import sqlite3
import threading
import time
import uuid
from ..errors import VoiceError


class EditStore:
    def __init__(self, root):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "audio-edits.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
                    created REAL NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
                    audio TEXT, parameters TEXT NOT NULL, details TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS edit_versions_project ON versions(project_id, created);
            """)
            self.db.execute("UPDATE versions SET status='failed', error=? WHERE status IN ('queued','running')",
                            ("上次编辑被中断，请重新处理；原音频和完成的版本仍保留。",))

    def create(self, state, audio, details):
        identity, clip_id, version = (uuid.uuid4().hex for _ in range(3))
        state = {**state, "selected_version": version}
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (identity, clip_id, now, now, json.dumps(state)))
            self.db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (version, identity, None, now, "import", "done", audio, json.dumps({"state": state}), json.dumps(details), None))
        return identity

    def get(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM projects WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("EDIT_PROJECT_NOT_FOUND", "音频编辑项目不存在", stage="音频编辑")
            versions = []
            for item in self.db.execute("SELECT * FROM versions WHERE project_id=? ORDER BY created DESC, rowid DESC", (identity,)):
                v = dict(item)
                v["parameters"] = json.loads(v["parameters"])
                v["details"] = json.loads(v["details"]) if v["details"] else None
                versions.append(v)
            return {**dict(row), "state": json.loads(row["state"]), "versions": versions}

    def recent(self):
        with self.lock:
            return [{"id": r["id"], "clip_id": r["clip_id"], "created": r["created"], "updated": r["updated"],
                     "name": json.loads(r["state"])["name"]}
                    for r in self.db.execute("SELECT * FROM projects ORDER BY updated DESC LIMIT 200")]

    def save(self, identity, state):
        with self.lock, self.db:
            project = self.get(identity)
            version = state.get("selected_version")
            if version and not any(v["id"] == version and v["status"] == "done" for v in project["versions"]):
                raise VoiceError("EDIT_VERSION_INVALID", "只能选择本项目已完成的音频版本")
            self.db.execute("UPDATE projects SET state=?, updated=? WHERE id=?", (json.dumps(state), time.time(), identity))

    def enqueue(self, identity, parameters):
        version = uuid.uuid4().hex
        with self.lock, self.db:
            project = self.get(identity)
            parent = parameters["source_version"]
            if not any(v["id"] == parent and v["status"] == "done" for v in project["versions"]):
                raise VoiceError("EDIT_VERSION_INVALID", "处理来源必须是本项目已完成的音频版本")
            self.save(identity, parameters["state"])
            self.db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (version, identity, parent, time.time(), parameters["operation"], "queued", None,
                             json.dumps(parameters), None, None))
        return version

    def finish(self, version, status, *, audio=None, details=None, error=None):
        with self.lock, self.db:
            self.db.execute("UPDATE versions SET status=?, audio=?, details=?, error=? WHERE id=?",
                            (status, audio, json.dumps(details) if details else None, error, version))

    def close(self):
        self.db.close()

    def unfinished_files(self):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT id FROM versions WHERE status IN ('failed','cancelled') AND audio IS NULL")]

    def has_audio(self, filename):
        with self.lock:
            return self.db.execute("SELECT 1 FROM versions WHERE audio=? AND status='done' LIMIT 1", (filename,)).fetchone() is not None
