"""Keep installed-app data independent of replacement program files."""
from __future__ import annotations

from datetime import datetime
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import urllib.request

from .runtime import frozen, project_root


def default_data_dir():
    if not frozen():
        return project_root() / "data"
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return local / "声笺" / "data"


def rebase_paths(value, source, target):
    """Rebase absolute managed paths; leave external assets and ordinary text intact."""
    if isinstance(value, dict):
        return {key: rebase_paths(item, source, target) for key, item in value.items()}
    if isinstance(value, list):
        return [rebase_paths(item, source, target) for item in value]
    if isinstance(value, str) and '\n' not in value:
        try:
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(source):
                return str(target / path.relative_to(source))
        except (ValueError, OSError):
            pass
    return value


def snapshot_database(source, target):
    """Include committed WAL data without opening the source for writing."""
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
        with closing(sqlite3.connect(target)) as new, new:
            old.backup(new)
            if new.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("旧数据库完整性检查未通过，已保留原数据。")


def _rebase_config(value, source, target):
    updated = rebase_paths(value, source, target)
    _relink_reference_copies(value, updated, source, target)
    if 'removed_models' in updated:
        updated['removed_models'] = [os.path.normcase(str(Path(path).resolve())) for path in updated['removed_models']]
    ids = {}
    for old, new in zip(value.get('references', []), updated.get('references', [])):
        if old['reference_audio'] != new['reference_audio']:
            new['id'] = hashlib.sha256(os.path.normcase(str(Path(new['reference_audio']).resolve())).encode()).hexdigest()[:24]
            ids[old['id']] = new['id']
    deleted = {}
    for old_id, item in updated.get('deleted_references', {}).items():
        previous = value['deleted_references'][old_id]
        if previous['reference_audio'] != item['reference_audio']:
            item['id'] = hashlib.sha256(os.path.normcase(str(Path(item['reference_audio']).resolve())).encode()).hexdigest()[:24]
            ids[old_id] = item['id']
        deleted[item['id']] = item
    if 'deleted_references' in updated:
        updated['deleted_references'] = deleted
    if 'hidden_references' in updated:
        updated['hidden_references'] = [ids.get(item, item) for item in updated['hidden_references']]
    return updated


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.digest()


def _relink_reference_copies(original, updated, source, target):
    # Older copied packages can retain absolute references to the build folder.
    # Prefer a byte-identical local copy, so those presets no longer depend on it.
    if isinstance(original, dict):
        for field in ('reference_audio', 'emotion_reference_audio'):
            audio = original.get(field)
            if audio:
                previous = Path(audio)
                local = source / 'voices' / previous.name
                if local.is_file() and previous.is_file() and (previous.resolve() == local.resolve() or _digest(local) == _digest(previous)):
                    updated[field] = str(target / 'voices' / previous.name)
        for key, item in original.items():
            if isinstance(item, (dict, list)):
                _relink_reference_copies(item, updated[key], source, target)
    elif isinstance(original, list):
        for before, after in zip(original, updated):
            _relink_reference_copies(before, after, source, target)


def _legacy_in_use(source, port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            data = json.load(response)
        return data.get("service") == "local-voice-workbench" and Path(data.get("data_dir", "")).resolve() == source
    except (OSError, ValueError):
        return False


def migrate_legacy_data(source, target):
    """Copy and rebase a portable database, then publish; never replace saved data."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or not (source / "config.json").is_file():
        return False
    if (target / "config.json").exists() or (target / "tasks.sqlite3").exists():
        return False
    if target.exists() and any(target.iterdir()):
        raise RuntimeError(f"新的数据目录已有文件，未覆盖。请检查：{target}\n旧数据仍保留在：{source}")
    from .manager import ServiceLock
    lock = ServiceLock(source)
    try:
        return _copy_legacy_data(source, target)
    finally:
        lock.close()


def _copy_legacy_data(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    total = sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
    if shutil.disk_usage(target.parent).free < total + 64 * 1024 ** 2:
        raise RuntimeError(f"迁移需要约 {total / 1024 ** 3:.2f} GB 可用空间。旧数据仍保留在：{source}")
    stage = Path(tempfile.mkdtemp(prefix=".data-migration-", dir=target.parent))
    try:
        database = source / "tasks.sqlite3"
        shutil.copytree(source, stage, dirs_exist_ok=True, symlinks=True,
                        ignore=lambda directory, names: [n for n in names if n in
                            ("tasks.sqlite3", "tasks.sqlite3-wal", "tasks.sqlite3-shm", "service.lock")]
                        if Path(directory).resolve() == source else [])
        if any(p.is_symlink() for p in stage.rglob('*')):
            raise RuntimeError("旧数据包含链接目录，未自动迁移。请使用 --data-dir 指定原数据目录。")
        # Runtime/model packages are verified assets; only rewrite our own state files.
        for path in stage.glob('*.json'):
            value = json.loads(path.read_text(encoding="utf-8"))
            updated = _rebase_config(value, source, target) if path.name == 'config.json' else rebase_paths(value, source, target)
            path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        if database.is_file():
            snapshot_database(database, stage / database.name)
            with closing(sqlite3.connect(stage / database.name)) as db, db:
                for table, column in (("jobs", "voice"), ("segments", "voice"), ("segments", "provenance")):
                    columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                    if column not in columns:
                        continue
                    for rowid, payload in db.execute(f"SELECT rowid,{column} FROM {table} WHERE {column} IS NOT NULL").fetchall():
                        value = json.dumps(_rebase_config(json.loads(payload), source, target), ensure_ascii=False)
                        db.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (value, rowid))
                if db.execute("SELECT name FROM sqlite_master WHERE name='cache'").fetchone():
                    for key, path in db.execute("SELECT key,path FROM cache").fetchall():
                        db.execute("UPDATE cache SET path=? WHERE key=?", (rebase_paths(path, source, target), key))
        (stage / "migration.json").write_text(json.dumps({"source": str(source), "created": datetime.now().isoformat()},
                                                         ensure_ascii=False, indent=2), encoding="utf-8")
        if target.exists():
            target.rmdir()  # An empty destination only; never remove a concurrent launch's data.
        stage.rename(target)
        return True
    finally:
        if stage.exists() and stage.name.startswith('.data-migration-') and stage.resolve().parent == target.parent:
            shutil.rmtree(stage)


def prepare_data_dir(port=8765):
    target = default_data_dir().resolve()
    if frozen() and not ((target / "config.json").exists() or (target / "tasks.sqlite3").exists()):
        source = (project_root() / "data").resolve()
        if (source / "config.json").is_file():
            # Keep a running legacy service and its downloads in the same directory until exit.
            if _legacy_in_use(source, port):
                return source
            migrate_legacy_data(source, target)
    return target
