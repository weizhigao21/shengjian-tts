from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .naming import UNNAMED, work_title, work_type, display_title
from .services.dialogue import role_names

WORKFLOW_TITLES = ("双语工作流配音", "工作流兼容请求")
WORKFLOW_AUDIO_TTL = 3600


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
            filters = ["j.deleted=0"] if "deleted" in columns else []
            if "record_only" in columns:
                filters.append("j.record_only=0")
            else:
                # Hide old adapter records even before the service performs its migration.
                filters.append("j.title NOT IN ('双语工作流配音','工作流兼容请求')")
                if "workflow_key" in columns:
                    filters.append("j.workflow_key IS NULL")
            visible = "WHERE " + " AND ".join(filters)
            rows = db.execute(f"""SELECT j.*,count(s.idx) AS total,
                COALESCE(SUM(CASE WHEN s.status='done' THEN 1 ELSE 0 END),0) AS completed,
                COALESCE(SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END),0) AS failed
                FROM jobs j LEFT JOIN segments s ON s.job_id=j.id {segment_filter} {visible} GROUP BY j.id
                ORDER BY j.created DESC LIMIT ?""", (limit,))
            jobs = [dict(row) for row in rows]
            for job in jobs:
                job["voice"] = json.loads(job["voice"])
                job["script_mentions"] = json.loads(job.get("script_mentions") or "[]")
                job["editor_state"] = json.loads(job.get("editor_state") or "null")
                job["work_type"], job["display_title"] = work_type(job), display_title(job)
            return jobs

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        path = self.root / "tasks.sqlite3"
        if path.is_file():
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as existing:
                columns = {r[1] for r in existing.execute("PRAGMA table_info(jobs)")}
                segment_columns = {r[1] for r in existing.execute("PRAGMA table_info(segments)")}
            if columns and ("script_mentions" not in columns or "editor_state" not in columns or "content_type" not in segment_columns):
                from .data_paths import snapshot_database
                prefix = ("before-voice-mentions-" if "script_mentions" not in columns else
                          "before-dialogue-editor-" if "editor_state" not in columns else "before-dialogue-scope-")
                backup = self.root / "backups" / (prefix + str(time.time_ns()) + ".sqlite3")
                backup.parent.mkdir(parents=True, exist_ok=True)
                snapshot_database(path.resolve(), backup)
        self.db = sqlite3.connect(self.root / "tasks.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        migrate_types = "content_type" not in {r[1] for r in self.db.execute("PRAGMA table_info(segments)")}
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
            "jobs": {"deleted": "INTEGER NOT NULL DEFAULT 0", "script": "TEXT", "script_mentions": "TEXT", "editor_state": "TEXT", "auto_named": "INTEGER NOT NULL DEFAULT 0", "workflow_key": "TEXT", "workflow_closed": "INTEGER NOT NULL DEFAULT 0", "record_only": "INTEGER NOT NULL DEFAULT 0"},
            "segments": {"voice": "TEXT", "fingerprint": "TEXT", "batch_title": "TEXT",
                         "created": "REAL", "generation": "INTEGER NOT NULL DEFAULT 1",
                         "provenance": "TEXT", "source_id": "TEXT", "source_revision": "TEXT", "force_generate": "INTEGER NOT NULL DEFAULT 0",
                         "content_type": "TEXT NOT NULL DEFAULT 'article'",
                         "duration": "REAL", "generation_seconds": "REAL", "audio_deleted": "INTEGER NOT NULL DEFAULT 0", "workflow": "INTEGER NOT NULL DEFAULT 0",
                         "audio_storage": "TEXT NOT NULL DEFAULT 'permanent'", "delivery_ack": "INTEGER NOT NULL DEFAULT 0",
                         "audio_expires": "REAL", "audio_released": "REAL", "audio_release_reason": "TEXT",
                         "inference_mode": "TEXT NOT NULL DEFAULT 'auto'", "inference": "TEXT",
                         "queue_paused": "INTEGER NOT NULL DEFAULT 0"},
        }.items():
            existing = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, declaration in fields.items():
                if name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        self.db.execute("""UPDATE jobs SET record_only=1 WHERE workflow_key IS NOT NULL
            OR title IN ('双语工作流配音','工作流兼容请求')""")
        # Freeze old segment snapshots before a later generation changes the work's voice.
        self.db.execute("""UPDATE segments SET
            voice=COALESCE(voice,(SELECT voice FROM jobs WHERE id=job_id)),
            fingerprint=COALESCE(fingerprint,(SELECT fingerprint FROM jobs WHERE id=job_id)),
            batch_title=COALESCE(batch_title,(SELECT title FROM jobs WHERE id=job_id)),
            created=COALESCE(created,(SELECT created FROM jobs WHERE id=job_id))""")
        self.db.commit()
        if migrate_types:
            with self.db:
                self.db.execute("UPDATE segments SET content_type='dialogue' WHERE source_id IS NOT NULL")
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS dialogue_audio_history ON segments(job_id,source_id,idx);
            CREATE VIEW IF NOT EXISTS article_audio_records AS SELECT * FROM segments WHERE content_type='article';
            CREATE VIEW IF NOT EXISTS dialogue_audio_records AS SELECT * FROM segments WHERE content_type='dialogue';
            CREATE INDEX IF NOT EXISTS workflow_jobs ON jobs(workflow_key);
            CREATE INDEX IF NOT EXISTS queued_segments ON segments(status,job_id,idx);
            CREATE INDEX IF NOT EXISTS generation_segments ON segments(job_id,generation);
            CREATE INDEX IF NOT EXISTS workflow_audio_expiry ON segments(audio_expires);
        """)

    def recover(self):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='queued' WHERE status='running'")
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    def create(self, title, voice, fingerprint, segments, script=None, provenance=None, workflow_key=None,
               record_only=False, delivery_ack=False, script_mentions=None, editor_state=None):
        job_id = uuid.uuid4().hex
        now = time.time()
        automatic = int(title.strip() in UNNAMED)
        title = work_title(title, script, now, segments)
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs(id,title,voice,fingerprint,status,created,updated,script,script_mentions,editor_state,auto_named,workflow_key,record_only) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (job_id, title, json.dumps(voice, ensure_ascii=False), fingerprint, "draft", now, now, script,
                             json.dumps(script_mentions or [], ensure_ascii=False), json.dumps(editor_state, ensure_ascii=False),
                             automatic, workflow_key, int(record_only or bool(workflow_key))))
            if segments:
                self.append(job_id, title, voice, fingerprint, segments, script, provenance,
                            workflow=bool(workflow_key) or record_only, delivery_ack=delivery_ack, script_mentions=script_mentions,
                            editor_state=editor_state)
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
            job = self.get(job_id)
            return {"job_id": job_id, "title": title, "display_title": job["display_title"], "generation": generation, "segment_indices": indices,
                    "status_url": f"/jobs/{job_id}?generation={generation}"}

    def append(self, job_id, title, voice, fingerprint, segments, script=None, provenance=None, workflow=False, delivery_ack=False, script_mentions=None, editor_state=None):
        now = time.time()
        snapshot = json.dumps(voice, ensure_ascii=False)
        with self.lock, self.db:
            editor_state = self.fixed_editor_state(job_id, editor_state)
            title, automatic = self.document_title(job_id, title, script, segments)
            record_only = self.db.execute("SELECT record_only FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            last, generation = self.db.execute("SELECT COALESCE(MAX(idx),0),COALESCE(MAX(generation),0) FROM segments WHERE job_id=?", (job_id,)).fetchone()
            self.db.executemany("""INSERT INTO segments
                (job_id,idx,text,timestamp,status,voice,fingerprint,batch_title,created,generation,provenance,workflow,audio_storage,delivery_ack,source_id,source_revision,force_generate,content_type,inference_mode)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [(job_id, last+i, s["text"], s.get("timestamp", ""),
                "queued", json.dumps(s["prepared_voice"].voice, ensure_ascii=False) if s.get("prepared_voice") else snapshot,
                s["prepared_voice"].fingerprint if s.get("prepared_voice") else fingerprint, title, now, generation+1,
                (json.dumps(s["prepared_voice"].provenance if s.get("prepared_voice") else provenance, ensure_ascii=False)
                 if s.get("prepared_voice") or provenance is not None else None), int(workflow),
                "temporary" if record_only else "permanent", int(delivery_ack and record_only),
                s.get("source_id"), s.get("source_revision"), int(s.get("force_generate", False)),
                "dialogue" if s.get("source_id") else "article", s.get("inference_mode", "auto")) for i, s in enumerate(segments, 1)])
            self.db.execute("UPDATE jobs SET title=?,voice=?,fingerprint=?,script=?,script_mentions=?,editor_state=?,updated=?,auto_named=? WHERE id=?",
                            (title, snapshot, fingerprint, script, json.dumps(script_mentions or [], ensure_ascii=False),
                             json.dumps(editor_state, ensure_ascii=False), now, automatic, job_id))
            self._refresh(job_id)

    def document_title(self, job_id, title, script, segments=()):
        row = self.db.execute("SELECT title,created,auto_named FROM jobs WHERE id=?", (job_id,)).fetchone()
        automatic = title.strip() in UNNAMED or (row["auto_named"] and title.strip() == row["title"])
        result = work_title("" if automatic else title, script, row["created"], segments)
        if automatic and result == "未命名配音" and row["auto_named"]:
            result = row["title"]
        return result, int(automatic)

    def save_document(self, job_id, title, script, script_mentions=None, editor_state=None):
        with self.lock, self.db:
            editor_state = self.fixed_editor_state(job_id, editor_state)
            title, automatic = self.document_title(job_id, title, script)
            self.db.execute("UPDATE jobs SET title=?,script=?,script_mentions=?,editor_state=?,updated=?,auto_named=? WHERE id=? AND deleted=0",
                            (title, script, json.dumps(script_mentions or [], ensure_ascii=False), json.dumps(editor_state, ensure_ascii=False),
                             time.time(), automatic, job_id))
        return title

    def fixed_editor_state(self, job_id, editor_state):
        """Preserve a work's creation type across saves and appended generations."""
        with self.lock:
            row = self.db.execute("SELECT editor_state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                from .errors import VoiceError
                raise VoiceError("JOB_NOT_FOUND", "任务不存在", stage="文稿保存")
            existing = json.loads(row["editor_state"] or "null")
            if editor_state is None:
                return existing
            if work_type({"editor_state": editor_state}) != work_type({"editor_state": existing}):
                from .errors import VoiceError
                raise VoiceError("TASK_TYPE_LOCKED", "任务创建后不能切换文章或对话类型", "请返回创作入口，新建另一种类型的任务。", "任务类型")
            return editor_state

    def get(self, job_id, include_deleted=False, generation=None):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or (row["deleted"] and not include_deleted):
                return None
            job = dict(row)
            job["voice"] = json.loads(job["voice"])
            job["script_mentions"] = json.loads(job.get("script_mentions") or "[]")
            job["editor_state"] = json.loads(job.get("editor_state") or "null")
            job["work_type"], job["display_title"] = work_type(job), display_title(job)
            items = [dict(s) for s in self.db.execute("SELECT * FROM segments WHERE job_id=? AND audio_deleted=0 AND (? IS NULL OR generation=?) ORDER BY idx", (job_id, generation, generation))]
            names = role_names(job["editor_state"])
            for item in items:
                item["error"] = json.loads(item["error"]) if item["error"] else None
                item["voice"] = json.loads(item["voice"]) if item["voice"] else job["voice"]
                item["fingerprint"] = item["fingerprint"] or job["fingerprint"]
                item["provenance"] = json.loads(item["provenance"]) if item["provenance"] else None
                item["inference"] = json.loads(item["inference"]) if item["inference"] else None
                if item.get("source_id") in names:
                    item["source_name"] = names[item["source_id"]]
            job["segments"] = items
            job["total"] = len(items)
            job["completed"] = sum(s["status"] == "done" for s in items)
            job["failed"] = sum(s["status"] == "failed" for s in items)
            if generation is not None and items:
                states = {s["status"] for s in items}
                job["status"] = next(s for s in ("running", "queued", "failed", "cancelled", "done") if s in states)
            return job

    def queued_job_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM jobs WHERE deleted=0 AND status IN ('queued','running')").fetchone()[0]

    def list(self, limit=100, include_workflow=False):
        with self.lock:
            jobs = [dict(row) for row in self.db.execute("""SELECT j.*,count(s.idx) AS total,
                COALESCE(SUM(CASE WHEN s.status='done' THEN 1 ELSE 0 END),0) AS completed,
                COALESCE(SUM(CASE WHEN s.status='failed' THEN 1 ELSE 0 END),0) AS failed
                FROM jobs j LEFT JOIN segments s ON s.job_id=j.id AND s.audio_deleted=0
                WHERE j.deleted=0 AND (j.record_only=0 OR ?) GROUP BY j.id
                ORDER BY j.created DESC LIMIT ?""", (int(include_workflow), limit))]
            for job in jobs:
                job["voice"] = json.loads(job["voice"])
                job["script_mentions"] = json.loads(job.get("script_mentions") or "[]")
                job["editor_state"] = json.loads(job.get("editor_state") or "null")
                job["work_type"], job["display_title"] = work_type(job), display_title(job)
            return jobs

    def pending(self):
        """Anything still waiting for the queue, batched or native."""
        with self.lock, self.db:
            return self.db.execute("""SELECT 1 FROM segments s JOIN jobs j ON j.id=s.job_id
                WHERE s.status IN ('queued','running') AND s.queue_paused=0 AND j.deleted=0
                AND j.status IN ('queued','running') LIMIT 1""").fetchone() is not None

    def next(self, job_id=None, admitted=None):
        with self.lock, self.db:
            row = self.db.execute("""SELECT s.job_id,s.idx FROM segments s JOIN jobs j ON j.id=s.job_id
                WHERE s.status='queued' AND s.queue_paused=0 AND j.deleted=0 AND j.status IN ('queued','running')
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
            self.db.execute("UPDATE segments SET status='running',attempts=attempts+1,error=NULL,generation_seconds=NULL,inference=NULL WHERE job_id=? AND idx=?", tuple(row))
            self.db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row[0]))
            job["status"] = item["status"] = "running"
            item["attempts"] += 1
            item["error"] = None
            item["generation_seconds"] = None
            item["provenance"] = json.loads(item["provenance"]) if item["provenance"] else None
            return job, item

    def claim_batch(self, job, first, compatible, limit, max_chars, minimum=2):
        """Atomically reserve compatible rows from this generation, with bounded lookahead."""
        with self.lock, self.db:
            selected, chars = [first], len(first["text"])
            rows = self.db.execute("""SELECT * FROM segments WHERE job_id=? AND generation=? AND status='queued' AND queue_paused=0
                ORDER BY idx LIMIT 64""", (job["id"], first["generation"])).fetchall()
            for row in rows:
                item = dict(row)
                item["voice"] = json.loads(item["voice"]) if item["voice"] else job["voice"]
                item["fingerprint"] = item["fingerprint"] or job["fingerprint"]
                if chars + len(item["text"]) > max_chars or not compatible(item):
                    continue
                item["provenance"] = json.loads(item["provenance"]) if item["provenance"] else None
                selected.append(item)
                chars += len(item["text"])
                if len(selected) >= limit:
                    break
            if len(selected) < minimum:
                return []
            for item in selected[1:]:
                self.db.execute("""UPDATE segments SET status='running',attempts=attempts+1,error=NULL,
                    generation_seconds=NULL,inference=NULL WHERE job_id=? AND idx=? AND status='queued'""", (job["id"], item["idx"]))
                item.update(status="running", attempts=item["attempts"]+1, error=None)
            return selected

    def finish(self, job_id, index, *, audio="", error=None, cached=False, duration=None, generation_seconds=None, inference=None):
        with self.lock, self.db:
            self.db.execute("""UPDATE segments SET status=?,audio=?,error=?,cached=?,duration=?,generation_seconds=?,inference=?
                WHERE job_id=? AND idx=? AND status='running'""",
                ("failed" if error else "done", audio, json.dumps(error, ensure_ascii=False) if error else None,
                 int(cached), duration, generation_seconds, json.dumps(inference) if inference else None, job_id, index))
            self._refresh(job_id)

    def _refresh(self, job_id):
        states = {r[0] for r in self.db.execute("SELECT status FROM segments WHERE job_id=? AND audio_deleted=0 GROUP BY status", (job_id,))}
        state = "draft" if not states else "running" if "running" in states else "queued" if "queued" in states else "failed" if "failed" in states else "cancelled" if "cancelled" in states else "done"
        self.db.execute("UPDATE jobs SET status=?,updated=? WHERE id=?", (state, time.time(), job_id))
        # A large batch can take hours. Start the download grace period only when
        # its own generation is terminal, rather than when the first clip finishes.
        self.db.execute("""UPDATE segments SET audio_expires=? WHERE job_id=?
            AND audio_storage='temporary' AND audio IS NOT NULL AND audio!='' AND audio_expires IS NULL
            AND generation NOT IN (SELECT generation FROM segments WHERE job_id=? AND status IN ('queued','running'))""",
            (time.time() + WORKFLOW_AUDIO_TTL, job_id, job_id))

    def requeue(self, job_id, index):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='queued',error=NULL WHERE job_id=? AND idx=? AND status='running'", (job_id, index))
            self._refresh(job_id)

    def pause_generation(self, job_id, generation, paused):
        with self.lock, self.db:
            cursor = self.db.execute("""UPDATE segments SET queue_paused=? WHERE job_id=? AND generation=?""",
                                     (int(paused), job_id, generation))
            return bool(cursor.rowcount)

    def generation_progress(self, job_id, generation, pending=False):
        """Compact completion feed; never deserialize all voice/model snapshots.

        pending=True only returns rows the workflow still has to take: finished
        audio that was not released yet, plus failures/cancellations. This keeps
        long subtitle runs from re-sending every completed segment on each poll.
        """
        with self.lock:
            counts = self.db.execute("""SELECT s.status,COUNT(*) AS n,
                SUM(CASE WHEN s.queue_paused=1 AND s.status IN ('queued','running') THEN 1 ELSE 0 END) AS paused
                FROM segments s JOIN jobs j ON j.id=s.job_id
                WHERE s.job_id=? AND s.generation=? AND j.deleted=0 GROUP BY s.status""",
                (job_id, generation)).fetchall()
            if not counts:
                return None
            states = {r['status'] for r in counts}
            if pending:
                condition = ("(s.status='done' AND s.audio IS NOT NULL AND s.audio_deleted=0) "
                             "OR s.status IN ('failed','cancelled')")
            else:
                condition = "s.status IN ('done','failed','cancelled')"
            rows = self.db.execute(f"""SELECT s.idx,s.status,s.audio,s.error,s.audio_storage FROM segments s
                JOIN jobs j ON j.id=s.job_id
                WHERE s.job_id=? AND s.generation=? AND j.deleted=0 AND ({condition}) ORDER BY s.idx""",
                (job_id, generation)).fetchall()
            status = next(s for s in ('running','queued','failed','cancelled','done') if s in states)
            return {'job_id': job_id, 'generation': generation, 'status': status,
                    'total': sum(r['n'] for r in counts),
                    'paused': any(r['paused'] for r in counts),
                    'completed': sum(r['n'] for r in counts if r['status'] == 'done'),
                    'segments': [{'idx': r['idx'], 'status': r['status'], 'audio': r['audio'], 'audio_storage': r['audio_storage'],
                                  'error': json.loads(r['error']) if r['error'] else None}
                                 for r in rows]}

    def cancel(self, job_id, generation=None):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='cancelled',queue_paused=0 WHERE job_id=? AND status IN ('queued','running') AND (? IS NULL OR generation=?)", (job_id, generation, generation))
            if generation is None:
                self.db.execute("UPDATE jobs SET workflow_closed=1 WHERE id=? AND workflow_key IS NOT NULL", (job_id,))
            self._refresh(job_id)

    def retry(self, job_id):
        with self.lock, self.db:
            self.db.execute("UPDATE segments SET status='queued',queue_paused=0,error=NULL,generation_seconds=NULL WHERE job_id=? AND status IN ('failed','cancelled')", (job_id,))
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
            rows = self.db.execute("""SELECT s.*,j.title AS work_title,j.deleted AS work_deleted,
                j.auto_named AS work_auto_named,j.editor_state AS work_editor_state
                FROM segments s JOIN jobs j ON j.id=s.job_id
                WHERE j.record_only=0 AND s.audio_deleted=0 AND s.status='done' AND s.audio IS NOT NULL AND s.audio!=''
                ORDER BY s.created DESC,s.job_id,s.idx DESC""")
            result = []
            documents = {}
            for row in rows:
                item = dict(row)
                item["id"] = f"{item['job_id']}:{item['idx']}"
                item["inference"] = json.loads(item["inference"]) if item["inference"] else None
                item["voice"] = json.loads(item["voice"] or "{}")
                item["provenance"] = json.loads(item["provenance"]) if item["provenance"] else None
                serialized = item.pop("work_editor_state")
                if item["job_id"] not in documents:
                    state = json.loads(serialized or "null")
                    documents[item["job_id"]] = state, role_names(state)
                state, names = documents[item["job_id"]]
                item["display_title"] = display_title({"title": item["work_title"], "auto_named": item.pop("work_auto_named"),
                    "editor_state": state})
                name = names.get(item.get("source_id"))
                if name:
                    item["source_name"] = name
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

    def workflow_audio_records(self, *, filename=None, job_id=None, generation=None, expired=False):
        with self.lock:
            rows = self.db.execute("""SELECT s.* FROM segments s JOIN jobs j ON j.id=s.job_id
                WHERE j.record_only=1 AND s.audio_deleted=0 AND s.audio IS NOT NULL AND s.audio!=''
                AND (? IS NULL OR s.audio=?) AND (? IS NULL OR s.job_id=?)
                AND (? IS NULL OR s.generation=?) AND (?=0 OR s.audio_expires<=?)""",
                (filename, filename, job_id, job_id, generation, generation, int(expired), time.time()))
            return [dict(r) for r in rows]

    def audio_references(self, filename, storage):
        with self.lock:
            return {f"{r[0]}:{r[1]}" for r in self.db.execute("""SELECT job_id,idx FROM segments
                WHERE audio=? AND audio_storage=? AND audio_deleted=0""", (filename, storage))}

    def reference_audio_paths(self):
        with self.lock:
            raw = {voice.get(field) for r in self.db.execute(
                "SELECT DISTINCT voice FROM segments WHERE voice IS NOT NULL AND voice!=''")
                for voice in [json.loads(r[0])] for field in ("reference_audio", "emotion_reference_audio")}
            return {Path(value).resolve() for value in raw if value}

    def release_workflow_audio(self, records, reason, cache_filenames=()):
        with self.lock, self.db:
            self.db.executemany("""UPDATE segments SET audio=NULL,audio_storage='released',
                audio_expires=NULL,audio_released=?,audio_release_reason=? WHERE job_id=? AND idx=?
                AND job_id IN (SELECT id FROM jobs WHERE record_only=1)""",
                [(time.time(), reason, r["job_id"], r["idx"]) for r in records])
            self.db.executemany("DELETE FROM cache WHERE path=?",
                               [(str(self.root / "audio" / filename),) for filename in cache_filenames])

    def workflow_records(self, limit=200):
        with self.lock:
            rows = self.db.execute("""SELECT s.*,j.title AS work_title FROM segments s
                JOIN jobs j ON j.id=s.job_id WHERE j.record_only=1
                ORDER BY s.created DESC,s.job_id,s.idx DESC LIMIT ?""", (limit,))
            result = []
            for row in rows:
                item = dict(row)
                for name in ("voice", "provenance", "error", "inference"):
                    item[name] = json.loads(item[name]) if item[name] else None
                result.append(item)
            return result

    def close(self):
        with self.lock:
            self.db.close()
