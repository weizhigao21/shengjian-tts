"""Read-only sidebar summaries for every creation mode, including before boot."""
from pathlib import Path
from contextlib import closing
import sqlite3

from .naming import work_type
from .store import Store

WORK_NAMES = {"article": "文章", "dialogue": "对话", "audio": "音频编辑", "recognition": "语音识别"}
WORK_ICONS = {"article": "article", "dialogue": "dialogue", "audio": "audio", "recognition": "recognize"}


def task_type(value):
    return value.get("task_type") or work_type(value)


def external_work(kind, identity, name, created, updated=None, status="done"):
    return {"id": kind + ":" + identity, "source_id": identity, "task_type": kind,
            "title": name, "display_title": name, "created": created, "updated": updated or created,
            "status": status, "total": 1, "completed": int(status == "done")}


def segment_audio_records(root, job_id, limit=200):
    """Finished audio of one work's latest generation, with the line that produced it."""
    path = Path(root).resolve() / "tasks.sqlite3"
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(segments)")}
            filters = ["job_id=?", "status='done'", "audio IS NOT NULL", "audio<>''"]
            if "audio_deleted" in columns:
                filters.append("audio_deleted=0")
            generation = "generation" if "generation" in columns else "1"
            created = "created" if "created" in columns else "NULL"
            text = "text" if "text" in columns else "''"
            order = f"{generation},idx" if generation == "generation" else "idx"
            rows = db.execute(f"SELECT audio,{generation},{text},{created} FROM segments"
                              f" WHERE {' AND '.join(filters)} ORDER BY {order} LIMIT ?",
                              (job_id, limit * 4)).fetchall()
    except sqlite3.Error:
        return []
    records = [{"audio": row[0], "generation": row[1] or 1, "text": row[2] or "", "created": row[3]}
               for row in rows if row[0]]
    if not records:
        return []
    latest = max(record["generation"] for record in records)
    return [record for record in records if record["generation"] == latest][:limit]


def segment_audio(root, job_id, limit=200):
    """Finished audio filenames of one work's latest generation, in segment order."""
    return [record["audio"] for record in segment_audio_records(root, job_id, limit)]


def read_recent(root, limit=100):
    root = Path(root).resolve()
    result = Store.read_recent(root, limit)
    queries = (
        ("audio", root / "audio-editor/audio-edits.sqlite3", """
            SELECT p.id,p.created,p.updated,json_extract(p.state,'$.name') AS name,
              COALESCE((SELECT v.status FROM versions v WHERE v.project_id=p.id
                        ORDER BY v.created DESC,v.rowid DESC LIMIT 1),'done') AS status
            FROM projects p ORDER BY p.created DESC LIMIT ?"""),
        ("recognition", root / "recognition/recognition.sqlite3", """
            SELECT id,created,updated,json_extract(state,'$.name') AS name,
                   json_extract(state,'$.status') AS status
            FROM sessions ORDER BY created DESC LIMIT ?"""),
    )
    for kind, path, query in queries:
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                db.row_factory = sqlite3.Row
                result.extend(external_work(kind, r["id"], r["name"] or "未命名音频", r["created"], r["updated"], r["status"] or "done")
                              for r in db.execute(query, (limit,)))
        except sqlite3.Error:
            # An unavailable independent mode must not hide the other saved works.
            continue
    return sorted(result, key=lambda r: (r["created"], r["id"]), reverse=True)[:limit]
