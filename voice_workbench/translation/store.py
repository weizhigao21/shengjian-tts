"""Terminology, translation memory and agent run records in their own SQLite file."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid

from ..errors import VoiceError

# Subtitle providers and MCP clients use both BCP-47 and base codes; store one form.
_LANGUAGE_CODES = {"en", "zh", "ja", "ko", "yue", "fr", "de", "es", "ru", "pt", "it"}
_LANGUAGE_ALIASES = {
    "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh", "zh-tw": "zh", "zh-hant": "zh", "zh-hk": "zh",
    "en-us": "en", "en-gb": "en", "en-au": "en", "ja-jp": "ja", "ko-kr": "ko",
    "fr-fr": "fr", "de-de": "de", "es-es": "es", "ru-ru": "ru",
    "pt-br": "pt", "pt-pt": "pt", "it-it": "it", "yue-hk": "yue", "yue-cn": "yue",
}


def normalize_language(value):
    """Map locale variants to the codes the translation UI and memory use."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    key = raw.replace("_", "-").lower()
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    base = key.split("-")[0]
    if base in _LANGUAGE_CODES:
        return base
    return raw



def normalized(text):
    """Exact-match key: casefold Latin text and collapse whitespace, keep punctuation."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def bounded(text, label, error_code, limit=500, required=False):
    value = str(text or "").strip()
    if required and not value:
        raise VoiceError(error_code, f"{label}不能为空", stage="翻译知识库")
    if len(value) > limit:
        raise VoiceError(error_code, f"{label}不能超过 {limit} 个字符", stage="翻译知识库")
    return value


class TranslationStore:
    def __init__(self, root):
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "translation.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS terms (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
                    source_lang TEXT NOT NULL DEFAULT '', target_lang TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS terms_unique
                    ON terms(source, source_lang, target_lang);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, source TEXT NOT NULL,
                    target TEXT NOT NULL, source_lang TEXT NOT NULL DEFAULT '',
                    target_lang TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                    quality INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS memories_unique
                    ON memories(source_key, source_lang, target_lang);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, trace TEXT NOT NULL DEFAULT '[]',
                    result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL);
            """)

    # ------------------------------------------------------------------- terms

    def add_term(self, source, target, source_lang="", target_lang="", note="", priority=0,
                 preserve_metadata=False):
        source = bounded(source, "术语原文", "TRANSLATION_TERM_INVALID", required=True)
        target = bounded(target, "术语译文", "TRANSLATION_TERM_INVALID", required=True)
        note = bounded(note, "术语说明", "TRANSLATION_TERM_INVALID")
        priority = max(0, min(100, int(priority or 0)))
        source_lang = normalize_language(source_lang)
        target_lang = normalize_language(target_lang)
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM terms WHERE source=? AND source_lang=? AND target_lang=?",
                (source, str(source_lang or ""), str(target_lang or ""))).fetchone()
            if row:
                if preserve_metadata:
                    self.db.execute("UPDATE terms SET target=?, enabled=1, updated=? WHERE id=?",
                                    (target, now, row["id"]))
                else:
                    self.db.execute(
                        "UPDATE terms SET target=?, note=?, priority=?, enabled=1, updated=? WHERE id=?",
                        (target, note, priority, now, row["id"]))
                identity = row["id"]
            else:
                identity = uuid.uuid4().hex
                self.db.execute("INSERT INTO terms VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (identity, source, target, str(source_lang or ""), str(target_lang or ""),
                                 note, priority, 1, now, now))
        return self.get_term(identity)

    def get_term(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM terms WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("TRANSLATION_TERM_NOT_FOUND", "术语不存在", stage="术语库")
            return self._term(row)

    def update_term(self, identity, changes):
        allowed = {key: value for key, value in dict(changes or {}).items()
                   if key in ("target", "note", "priority", "enabled")}
        if "target" in allowed:
            allowed["target"] = bounded(allowed["target"], "术语译文", "TRANSLATION_TERM_INVALID", required=True)
        if "note" in allowed:
            allowed["note"] = bounded(allowed["note"], "术语说明", "TRANSLATION_TERM_INVALID")
        if "priority" in allowed:
            allowed["priority"] = max(0, min(100, int(allowed["priority"] or 0)))
        if "enabled" in allowed:
            allowed["enabled"] = 1 if allowed["enabled"] else 0
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM terms WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("TRANSLATION_TERM_NOT_FOUND", "术语不存在", stage="术语库")
            if allowed:
                values = {**self._term(row), **allowed, "updated": time.time()}
                self.db.execute(
                    "UPDATE terms SET target=?, note=?, priority=?, enabled=?, updated=? WHERE id=?",
                    (values["target"].strip(), values["note"], values["priority"],
                     1 if values["enabled"] else 0, values["updated"], identity))
        return self.get_term(identity)

    def disable_term(self, identity):
        return self.update_term(identity, {"enabled": False})

    def terms(self, query="", target="", enabled_only=True, limit=200, offset=0):
        target = normalize_language(target)
        sql, params = "SELECT * FROM terms WHERE 1=1", []
        if enabled_only:
            sql, params = sql + " AND enabled=1", params
        if target:
            sql, params = sql + " AND target_lang IN ('', ?)", params + [str(target)]
        if query:
            sql, params = sql + " AND (source LIKE ? OR target LIKE ? OR note LIKE ?)", params + [f"%{query}%"] * 3
        sql = sql + " ORDER BY priority DESC, updated DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(500, int(limit or 200))), max(0, int(offset or 0))])
        with self.lock:
            return [self._term(row) for row in self.db.execute(sql, params)]

    def matching_terms(self, lines, target, limit=100):
        """Terms whose source occurs in any line, in priority order."""
        values = [str(line or "") for line in lines]
        target = normalize_language(target)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM terms WHERE enabled=1 AND target_lang IN ('', ?) "
                "ORDER BY priority DESC, updated DESC LIMIT 500", (str(target),)).fetchall()
        found = []
        for row in rows:
            term = self._term(row)
            needle = term["source"].casefold()
            if needle and any(needle in value.casefold() for value in values):
                found.append(term)
            if len(found) >= limit:
                break
        return found

    @staticmethod
    def _term(row):
        return {"id": row["id"], "source": row["source"], "target": row["target"],
                "source_lang": row["source_lang"], "target_lang": row["target_lang"],
                "note": row["note"], "priority": row["priority"],
                "enabled": bool(row["enabled"]), "created": row["created"], "updated": row["updated"]}

    # ----------------------------------------------------------------- memory

    def remember(self, source, target, source_lang="", target_lang="", session_id="",
                 provider="", model="", quality=1, preserve_metadata=False):
        source = bounded(source, "记忆原文", "TRANSLATION_MEMORY_INVALID", required=True)
        target = bounded(target, "记忆译文", "TRANSLATION_MEMORY_INVALID", required=True)
        source_lang = normalize_language(source_lang)
        target_lang = normalize_language(target_lang)
        key = normalized(source)
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM memories WHERE source_key=? AND source_lang=? AND target_lang=?",
                (key, str(source_lang or ""), str(target_lang or ""))).fetchone()
            if row:
                if preserve_metadata:
                    self.db.execute(
                        "UPDATE memories SET source=?, target=?, quality=?, enabled=1, updated=? WHERE id=?",
                        (source, target, max(int(row["quality"]), max(1, min(2, int(quality or 1)))), now, row["id"]))
                else:
                    self.db.execute(
                        "UPDATE memories SET source=?, target=?, session_id=?, provider=?, model=?, "
                        "quality=?, enabled=1, updated=? WHERE id=?",
                        (source, target, str(session_id or ""), str(provider or ""), str(model or ""),
                         max(int(row["quality"]), max(1, min(2, int(quality or 1)))), now, row["id"]))
                identity = row["id"]
            else:
                identity = uuid.uuid4().hex
                self.db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (identity, key, source, target, str(source_lang or ""),
                                 str(target_lang or ""), str(session_id or ""), str(provider or ""),
                                 str(model or ""), max(1, min(2, int(quality or 1))), 0, 1, now, now))
        return self.get_memory(identity)

    def get_memory(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM memories WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("TRANSLATION_MEMORY_NOT_FOUND", "翻译记忆不存在", stage="翻译记忆")
            return self._memory(row)

    def update_memory(self, identity, changes):
        allowed = {key: value for key, value in dict(changes or {}).items()
                   if key in ("target", "quality", "enabled")}
        if "target" in allowed:
            allowed["target"] = bounded(allowed["target"], "记忆译文", "TRANSLATION_MEMORY_INVALID",
                                        required=True)
        if "quality" in allowed:
            allowed["quality"] = max(1, min(2, int(allowed["quality"] or 1)))
        if "enabled" in allowed:
            allowed["enabled"] = 1 if allowed["enabled"] else 0
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM memories WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("TRANSLATION_MEMORY_NOT_FOUND", "翻译记忆不存在", stage="翻译记忆")
            if allowed:
                values = {**self._memory(row), **allowed, "updated": time.time()}
                self.db.execute(
                    "UPDATE memories SET target=?, quality=?, enabled=?, updated=? WHERE id=?",
                    (values["target"], values["quality"], 1 if values["enabled"] else 0,
                     values["updated"], identity))
        return self.get_memory(identity)

    def find_memory(self, source, source_lang="", target_lang=""):
        source_lang = normalize_language(source_lang)
        target_lang = normalize_language(target_lang)
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM memories WHERE source_key=? AND source_lang=? AND target_lang=? AND enabled=1",
                (normalized(source), str(source_lang or ""), str(target_lang or ""))).fetchone()
            return self._memory(row) if row else None

    def memory(self, query="", target="", limit=200, enabled_only=True, offset=0):
        target = normalize_language(target)
        sql, params = "SELECT * FROM memories WHERE 1=1", []
        if enabled_only:
            sql, params = sql + " AND enabled=1", params
        if target:
            sql, params = sql + " AND target_lang IN ('', ?)", params + [str(target)]
        if query:
            sql, params = sql + " AND (source LIKE ? OR target LIKE ?)", params + [f"%{query}%"] * 2
        sql = sql + " ORDER BY updated DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(500, int(limit or 200))), max(0, int(offset or 0))])
        with self.lock:
            return [self._memory(row) for row in self.db.execute(sql, params)]

    def touch_memory(self, identity):
        with self.lock, self.db:
            self.db.execute("UPDATE memories SET uses=uses+1, updated=? WHERE id=?",
                            (time.time(), identity))

    def disable_memory(self, identity):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM memories WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("TRANSLATION_MEMORY_NOT_FOUND", "翻译记忆不存在", stage="翻译记忆")
            self.db.execute("UPDATE memories SET enabled=0, updated=? WHERE id=?", (time.time(), identity))
        return self.get_memory(identity)

    @staticmethod
    def _memory(row):
        return {"id": row["id"], "source": row["source"], "target": row["target"],
                "source_lang": row["source_lang"], "target_lang": row["target_lang"],
                "session_id": row["session_id"], "provider": row["provider"], "model": row["model"],
                "quality": row["quality"], "uses": row["uses"], "enabled": bool(row["enabled"]),
                "created": row["created"], "updated": row["updated"]}

    # -------------------------------------------------------------------- runs

    def create_run(self, session_id="", target="", provider=""):
        identity = uuid.uuid4().hex
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (identity, str(session_id or ""), str(target or ""), str(provider or ""),
                             "running", "[]", "{}", "", now, now))
            self.db.execute("DELETE FROM agent_runs WHERE id NOT IN "
                            "(SELECT id FROM agent_runs ORDER BY updated DESC LIMIT 100)")
        return self.get_run(identity)

    def update_run(self, identity, status=None, trace=None, result=None, error=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM agent_runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("TRANSLATION_RUN_NOT_FOUND", "翻译 Agent 运行记录不存在", stage="音频翻译")
            values = {"status": row["status"], "trace": json.loads(row["trace"]),
                      "result": json.loads(row["result"]), "error": row["error"]}
            if status is not None:
                values["status"] = str(status)
            if trace is not None:
                values["trace"] = trace
            if result is not None:
                values["result"] = result
            if error is not None:
                values["error"] = str(error)
            self.db.execute("UPDATE agent_runs SET status=?, trace=?, result=?, error=?, updated=? WHERE id=?",
                            (values["status"], json.dumps(values["trace"], ensure_ascii=False),
                             json.dumps(values["result"], ensure_ascii=False), values["error"],
                             time.time(), identity))
        return self.get_run(identity)

    def get_run(self, identity):
        with self.lock:
            row = self.db.execute("SELECT * FROM agent_runs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise VoiceError("TRANSLATION_RUN_NOT_FOUND", "翻译 Agent 运行记录不存在", stage="音频翻译")
            return self._run(row)

    def runs(self, session_id="", limit=100):
        sql, params = "SELECT * FROM agent_runs WHERE 1=1", []
        if session_id:
            sql, params = sql + " AND session_id=?", params + [str(session_id)]
        sql = sql + " ORDER BY updated DESC LIMIT ?"
        params.append(max(1, min(200, int(limit or 100))))
        with self.lock:
            return [self._run(row) for row in self.db.execute(sql, params)]

    @staticmethod
    def _run(row):
        return {"id": row["id"], "session_id": row["session_id"], "target": row["target"],
                "provider": row["provider"], "status": row["status"],
                "trace": json.loads(row["trace"]), "result": json.loads(row["result"]),
                "error": row["error"], "created": row["created"], "updated": row["updated"]}

    def close(self):
        self.db.close()
