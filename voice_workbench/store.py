from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .naming import UNNAMED, work_title


class Store:
    @staticmethod
    def read_recent(root: Path, limit=100):
        """Read saved works before the service boots, without migration or writes."""
        path = Path(root).resolve() / "tasks.sqlite3"
        if not path.is_file():
            return []
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2) as db:
            db.row_factory = sqlite3.Row
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            segment_columns = {r[1] for r in db.execute("PRAGMA table_info(segments)")}
            segment_filter = " AND s.audio_deleted=0" if "audio_deleted" in segment_columns else ""
            visible = "WHERE j.deleted=0" if "deleted" in columns else ""
            rows = db.execute(f"""SELECT j.*,count(s.idx) AS total,
                COALESCE(SUM(CASE WHEN s.status='done' THEN 1 ELSE 0 END),0) AS completed,
                COALESCE(SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END),0) AS failed
                FROM jobs j LEFT JOIN segments s ON s.job_id=j.id {segment_filter} {visible} GROUP BY j.id
                ORDER BY j.created DESC LIMIT ?""", (limit,))
            jobs = [dict(row) for row in rows]
            for job in jobs:
                job["voice"] = json.loads(job["voice"])
            return jobs

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "tasks.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, title TEXT, voice TEXT, fingerprint TEXT,
            status TEXT, created REAL, updated REAL);
          CREATE TABLE IF NOT EXISTS segments (
            job_id TEXT, idx INTEGER, text TEXT, timestamp TEXT, status TEXT,
            audio TEXT, error TEXT, attempts INTEGER DEFAULT 0, cached INTEGER DEFAULT 0,
            PRIMARY KEY(job_id, idx));
          CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, path TEXT, checksum TEXT);
        """)
        for table, fields in {
            "jobs": {"deleted": "INTEGER NOT NULL DEFAULT 0", "script": "TEXT", "auto_named": "INTEGER NOT NULL DEFAULT 0", "workflow_key": "TEXT", "workflow_closed": "INTEGER NOT NULL DEFAULT 0"},
            "segments": {"voice": "TEXT", "fingerprint": "TEXT", "batch_title": "TEXT",
                         "created": "REAL", "generation": "INTEGER NOT NULL DEFAULT 1",
                         "provenance": "TEXT", "duration": "REAL", "audio_deleted": "INTEGER NOT NULL DEFAULT 0", "workflow": "INTEGER NOT NULL DEFAULT 0"},
        }.items():
            existing = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, declaration in fields.items():
                if name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        # Freeze old segment snapshots before a later generation changes the work's voice.
        self.db.execute("""UPDATE segments SET
            voice=COALESCE(voice,(SELECT voice FROM jobs WHERE id=job_id)),
            fingerprint=COALESCE(fingerprint,(SELECT fingerprint FROM jobs WHERE id=job_id)),
            batch_title=COALESCE(batch_title,(SELECT title FROM jobs WHERE id=job_id)),
            created=COALESCE(created,(SELECT created FROM jobs WHERE id=job_id))""")
        self.db.commit()
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS workflow_jobs ON jobs(workflow_key);
            CREATE INDEX IF NOT EXISTS queued_segments ON segments(status,job_id,idx);
            CREATE INDEX IF NOT EXISTS generation_segments ON segments(job_id,generation);
        """)

    def recover(self):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='queued' WHERE status='running'")
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    def create(self, title, voice, fingerprint, segments, script=None, provenance=None, workflow_key=None):
        job_id = uuid.uuid4().hex
        now = time.time()
        automatic = int(title.strip() in UNNAMED)
        title = work_title(title, script, now, segments)
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs(id,title,voice,fingerprint,status,created,updated,script,auto_named,workflow_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (job_id, title, json.dumps(voice, ensure_ascii=False), fingerprint, "draft", now, now, script, automatic, workflow_key))
            if segments:
                self.append(job_id, title, voice, fingerprint, segments, script, provenance, workflow=bool(workflow_key))
        return job_id

    def workflow_job(self, key):
        with self.lock:
            row = self.db.execute("SELECT id,deleted,workflow_closed FROM jobs WHERE workflow_key=? ORDER BY created DESC LIMIT 1", (key,)).fetchone()
            if row and row["deleted"]:
                raise ValueError("工作流任务已删除，请重新启动这一轮配音")
            if row and row["workflow_closed"]:
                raise ValueError("工作流任务已取消，请重新启动这一轮配音")
            return row["id"] if row else None

    def submission(self, job_id):
        with self.lock:
            title = self.db.execute("SELECT title FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            generation = self.db.execute("SELECT MAX(generation) FROM segments WHERE job_id=?", (job_id,)).fetchone()[0]
            indices = [r[0] for r in self.db.execute("SELECT idx FROM segments WHERE job_id=? AND generation=? ORDER BY idx", (job_id, generation))]
            return {"job_id": job_id, "title": title, "generation": generation, "segment_indices": indices,
                    "status_url": f"/jobs/{job_id}?generation={generation}"}

    def append(self, job_id, title, voice, fingerprint, segments, script=None, provenance=None, workflow=False):
        now = time.time()
        snapshot = json.dumps(voice, ensure_ascii=False)
        with self.lock, self.db:
            title, automatic = self.document_title(job_id, title, script, segments)
            last, generation = self.db.execute("SELECT COALESCE(MAX(idx),0),COALESCE(MAX(generation),0) FROM segments WHERE job_id=?", (job_id,)).fetchone()
            self.db.executemany("""INSERT INTO segments
                (job_id,idx,text,timestamp,status,voice,fingerprint,batch_title,created,generation,provenance,workflow)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", [(job_id, last+i, s["text"], s.get("timestamp", ""),
                "queued", snapshot, fingerprint, title, now, generation+1,
                json.dumps(provenance, ensure_ascii=False) if provenance is not None else None, int(workflow)) for i, s in enumerate(segments, 1)])
            self.db.execute("UPDATE jobs SET title=?,voice=?,fingerprint=?,script=?,updated=?,auto_named=? WHERE id=?",
                            (title, snapshot, fingerprint, script, now, automatic, job_id))
            self._refresh(job_id)

    def document_title(self, job_id, title, script, segments=()):
        row = self.db.execute("SELECT title,created,auto_named FROM jobs WHERE id=?", (job_id,)).fetchone()
        automatic = title.strip() in UNNAMED or (row["auto_named"] and title.strip() == row["title"])
        result = work_title("" if automatic else title, script, row["created"], segments)
        if automatic and result == "未命名配音" and row["auto_named"]:
            result = row["title"]
        return result, int(automatic)

    def save_document(self, job_id, title, script):
        with self.lock, self.db:
            title, automatic = self.document_title(job_id, title, script)
            self.db.execute("UPDATE jobs SET title=?,script=?,updated=?,auto_named=? WHERE id=? AND deleted=0",
                            (title, script, time.time(), automatic, job_id))
        return title

    def get(self, job_id, include_deleted=False, generation=None):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or (row["deleted"] and not include_deleted):
                return None
            job = dict(row)
            job["voice"] = json.loads(job["voice"])
            items = [dict(s) for s in self.db.execute("SELECT * FROM segments WHERE job_id=? AND audio_deleted=0 AND (? IS NULL OR generation=?) ORDER BY idx", (job_id, generation, generation))]
            for item in items:
                item["error"] = json.loads(item["error"]) if item["error"] else None
                item["voice"] = json.loads(item["voice"]) if item["voice"] else job["voice"]
                item["fingerprint"] = item["fingerprint"] or job["fingerprint"]
                item["provenance"] = json.loads(item["provenance"]) if item["provenance"] else None
            job["segments"] = items
            job["total"] = len(items)
            job["completed"] = sum(s["status"] == "done" for s in items)
            job["failed"] = sum(s["status"] == "failed" for s in items)
            if generation is not None and items:
                states = {s["status"] for s in items}
                job["status"] = next(s for s in ("running", "queued", "failed", "cancelled", "done") if s in states)
            return job

    def list(self, limit=100):
        with self.lock:
            jobs = [dict(row) for row in self.db.execute("""SELECT j.*,count(s.idx) AS total,
                COALESCE(SUM(CASE WHEN s.status='done' THEN 1 ELSE 0 END),0) AS completed,
                COALESCE(SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END),0) AS failed
                FROM jobs j LEFT JOIN segments s ON s.job_id=j.id AND s.audio_deleted=0 WHERE j.deleted=0 GROUP BY j.id
                ORDER BY j.created DESC LIMIT ?""", (limit,))]
            for job in jobs:
                job["voice"] = json.loads(job["voice"])
            return jobs

    def next(self, job_id=None, admitted=None):
        with self.lock, self.db:
            row = self.db.execute("""SELECT s.job_id,s.idx FROM segments s JOIN jobs j ON j.id=s.job_id
                WHERE s.status='queued' AND j.deleted=0 AND j.status IN ('queued','running')
                AND (? IS NULL OR s.job_id=?)
                ORDER BY j.created, s.idx LIMIT 1""", (job_id, job_id)).fetchone()
            if row is None:
                return None
            job = dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (row[0],)).fetchone())
            job["voice"] = json.loads(job["voice"])
            item = dict(self.db.execute("SELECT * FROM segments WHERE job_id=? AND idx=?", tuple(row)).fetchone())
            item["voice"] = json.loads(item["voice"]) if item["voice"] else job["voice"]
            item["fingerprint"] = item["fingerprint"] or job["fingerprint"]
            if admitted and not admitted(job, item):
                return None
            self.db.execute("UPDATE segments SET status='running',attempts=attempts+1,error=NULL WHERE job_id=? AND idx=?", tuple(row))
            self.db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row[0]))
            job["status"] = item["status"] = "running"
            item["attempts"] += 1
            item["error"] = None
            item["provenance"] = json.loads(item["provenance"]) if item["provenance"] else None
            return job, item

    def finish(self, job_id, index, *, audio="", error=None, cached=False, duration=None):
        with self.lock, self.db:
            self.db.execute("""UPDATE segments SET status=?,audio=?,error=?,cached=?,duration=?
                WHERE job_id=? AND idx=? AND status='running'""",
                ("failed" if error else "done", audio, json.dumps(error, ensure_ascii=False) if error else None,
                 int(cached), duration, job_id, index))
            self._refresh(job_id)

    def _refresh(self, job_id):
        states = [r[0] for r in self.db.execute("SELECT status FROM segments WHERE job_id=? AND audio_deleted=0", (job_id,))]
        state = "draft" if not states else "running" if "running" in states else "queued" if "queued" in states else "failed" if "failed" in states else "cancelled" if "cancelled" in states else "done"
        self.db.execute("UPDATE jobs SET status=?,updated=? WHERE id=?", (state, time.time(), job_id))

    def requeue(self, job_id, index):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='queued',error=NULL WHERE job_id=? AND idx=? AND status='running'", (job_id, index))
            self._refresh(job_id)

    def cancel(self, job_id, generation=None):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='cancelled' WHERE job_id=? AND status IN ('queued','running') AND (? IS NULL OR generation=?)", (job_id, generation, generation))
            if generation is None:
                self.db.execute("UPDATE jobs SET workflow_closed=1 WHERE id=? AND workflow_key IS NOT NULL", (job_id,))
            self._refresh(job_id)

    def retry(self, job_id):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='queued',error=NULL WHERE job_id=? AND status IN ('failed','cancelled')", (job_id,))
            self._refresh(job_id)

    def delete(self, job_id):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='cancelled' WHERE job_id=? AND status IN ('queued','running')", (job_id,))
            self._refresh(job_id)
            self.db.execute("UPDATE jobs SET deleted=1 WHERE id=?", (job_id,))

    def restore(self, job_id):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET deleted=0,updated=? WHERE id=?", (time.time(), job_id))

    def rename(self, job_id, title):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET title=?,updated=?,auto_named=0 WHERE id=? AND deleted=0", (title, time.time(), job_id))

    def cache_get(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def cache_put(self, key, path, checksum):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, str(path), checksum))

    def generated_audio(self):
        with self.lock:
            rows = self.db.execute("""SELECT s.*,j.title AS work_title,j.deleted AS work_deleted
                FROM segments s JOIN jobs j ON j.id=s.job_id
                WHERE s.audio_deleted=0 AND s.status='done' AND s.audio IS NOT NULL AND s.audio!=''
                ORDER BY s.created DESC,s.job_id,s.idx DESC""")
            result = []
            for row in rows:
                item = dict(row)
                item["id"] = f"{item['job_id']}:{item['idx']}"
                item["voice"] = json.loads(item["voice"] or "{}")
                item["provenance"] = json.loads(item["provenance"]) if item["provenance"] else None
                result.append(item)
            return result

    def erase_audio_records(self, ids):
        with self.lock, self.db:
            jobs = set()
            for identity in ids:
                job_id, index = identity.split(":")
                self.db.execute("UPDATE segments SET audio_deleted=1,audio=NULL WHERE job_id=? AND idx=? AND status='done'", (job_id, int(index)))
                jobs.add(job_id)
            for job_id in jobs:
                self._refresh(job_id)

    def forget_cache(self, filename):
        with self.lock, self.db:
            self.db.execute("DELETE FROM cache WHERE path=?", (str(self.root / "audio" / filename),))

    def close(self):
        with self.lock:
            self.db.close()
