"""把本机要交接的文件整理成一份带校验值的清单。

清单只描述「哪些文件、多大、sha256、落到目标机的哪个目录」，不搬运内容。
构建过程是只读的：不改 config.json，不动数据库，也不删除任何东西。

体积上的取舍由 scope 决定。默认的 batch 只带批量生成真正会加载的东西，
因为主电脑里最占地方的部分恰恰是可以重算或重建的（pip 缓存、日志、
识别模型、audio_cpp 引擎的多个历史版本）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time

from ..config import atomic_json

SCHEMA = 1
CACHE_NAME = "digest-cache.json"

# 每个范围声明「额外的 data 子项」，基础集合（程序、音色、音频库、批量运行时）总是包含。
SCOPES = {
    "batch": {"label": "仅批量生成（无原生引擎）", "recognition": False, "engines": False, "discovered": False, "native": False},
    "recognition": {"label": "批量 + 识别与对齐", "recognition": True, "engines": False, "discovered": True, "native": False},
    "full": {"label": "全量镜像（含原生引擎）", "recognition": True, "engines": True, "discovered": True, "all_models": True, "native": True},
}

# 纯批量的目标机不需要原生引擎：批量后端吃的是 HF 目录，GGUF 与
# audiocpp_server.exe 只在单条生成和批量失败回退时才被打开。因此只在
# 带原生引擎的范围（全量镜像）里，才按「被预设引用就必须带上」处理。

# 这些顶层目录对任何范围都没有交接价值：缓存、日志、临时与事务残留。
SKIP_TOP = {
    "logs", "temporary", "backups", "benchmarks", "audio-editor", "handoff",
    "service.lock", "tasks.sqlite3-shm", "tasks.sqlite3-wal", "migration.json",
    "engine-server.json", "service.json", "float-panel.json", "pending-drafts.json",
    "removed_models.json",
}

# 批量运行时里这些子项是纯缓存，目标机会自己重建。
SKIP_RUNTIME = {
    "runtime/qwen-batch/pip-cache", "runtime/qwen-batch/downloads",
    "runtime/qwen-batch/logs", "runtime/downloads",
}

DIGEST_CHUNK = 4 * 1024 * 1024


def digest(path: Path, cache: dict | None = None) -> str:
    """Return the sha256 of one file, reusing an unchanged entry when possible."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = str(path)
    if cache is not None:
        known = cache.get(key)
        if (isinstance(known, dict) and known.get("size") == stat.st_size
                and known.get("mtime_ns") == stat.st_mtime_ns
                and isinstance(known.get("sha256"), str) and len(known["sha256"]) == 64):
            return known["sha256"]
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(DIGEST_CHUNK), b""):
                hasher.update(chunk)
    except OSError:
        return ""
    value = hasher.hexdigest()
    if cache is not None:
        cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": value}
    return value


def _load_cache(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _iter_files(root: Path):
    """Yield files under root, skipping junctions and anything that vanished."""
    for directory, folders, files in os.walk(root, followlinks=False):
        base = Path(directory)
        folders[:] = sorted(f for f in folders if not (base / f).is_symlink())
        for name in sorted(files):
            path = base / name
            if path.is_symlink():
                continue
            yield path


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _referenced_models(data_dir: Path) -> set[str]:
    """Absolute, case-folded model paths the current presets actually point at."""
    try:
        value = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    found = set()
    shelves = [value.get("voices", []), value.get("model_templates", []),
               value.get("deleted_voices", {}).values()]
    for shelf in shelves:
        for voice in shelf:
            for field in ("model_dir", "engine_executable", "repo_dir"):
                raw = voice.get(field) if isinstance(voice, dict) else None
                if raw:
                    found.add(os.path.normcase(os.path.abspath(str(raw))))
    # The batch backend reads its own pair out of settings rather than a preset.
    batch = value.get("batch_inference")
    if isinstance(batch, dict):
        for field in ("model_dir", "source_model"):
            raw = batch.get(field)
            if raw:
                found.add(os.path.normcase(os.path.abspath(str(raw))))
    return found


def _under(parent: Path, paths) -> bool:
    """Whether any referenced path lives below this directory."""
    head = os.path.normcase(os.path.abspath(str(parent))) + os.sep
    return any(path.startswith(head) for path in paths)


def _program_entries(install_dir: Path | None, cache: dict):
    """The frozen program itself; a source checkout has nothing to hand over here."""
    if install_dir is None:
        return []
    install_dir = Path(install_dir).resolve()
    entries = []
    for path in _iter_files(install_dir):
        relative = _relative(path, install_dir)
        head = relative.split("/", 1)[0]
        # data/ belongs to the old in-place layout; it is not part of the program.
        if head == "data":
            continue
        entries.append({"id": "program/" + relative, "group": "program", "rel": relative,
                        "target": "install", "size": path.stat().st_size,
                        "sha256": digest(path, cache) if cache is not None else "",
                        "source": str(path)})
    return entries


def _data_entries(data_dir: Path, scope: str, cache: dict, snapshot: Path | None):
    policy = SCOPES[scope]
    data_dir = Path(data_dir)
    models = _referenced_models(data_dir)
    entries = []

    def add(path: Path, group: str, target: str = "data", name: str | None = None):
        relative = name or _relative(path, data_dir)
        try:
            size = path.stat().st_size
        except OSError:
            return
        entries.append({"id": "data/" + relative, "group": group, "rel": relative,
                        "target": target, "size": size,
                        "sha256": digest(path, cache) if cache is not None else "",
                        "source": str(path)})

    def allowed_under(root: Path, relative: str) -> bool:
        return not any(relative == skip or relative.startswith(skip + "/") for skip in SKIP_RUNTIME)

    def walk(root: Path, group: str):
        if not root.is_dir():
            return
        for path in _iter_files(root):
            relative = _relative(path, data_dir)
            if not allowed_under(root, relative):
                continue
            if relative in SKIP_TOP:
                continue
            add(path, group)

    for name in sorted(p.name for p in data_dir.iterdir()):
        path = data_dir / name
        if name in SKIP_TOP:
            continue
        relative = name
        if path.is_file():
            if name == "config.json":
                add(path, "voice")
            elif name == "tasks.sqlite3":
                # Hand over a consistent snapshot, never a live database mid-write.
                # Without one the database is left out entirely: a copy that still
                # depends on an uncheckpointed WAL would be silently corrupt.
                if snapshot is not None and snapshot.is_file():
                    add(snapshot, "library", name="tasks.sqlite3")
            elif name == "scan.json":
                add(path, "model")
            continue
        if not path.is_dir():
            continue
        if name in ("voices", "audio", "workflow-audio"):
            walk(path, "voice" if name == "voices" else "library")
        elif name == "recognition":
            if policy["recognition"]:
                walk(path, "recognition")
        elif name == "models":
            for child in sorted(p for p in path.iterdir()):
                sub = f"models/{child.name}"
                if child.is_file():
                    continue
                if child.name == "qwen-batch":
                    # 0.6B and 1.7B are two complete weight sets; the batch backend
                    # loads exactly one. Carrying both doubles the model payload for
                    # a size that never runs, so only the referenced one travels.
                    sizes = sorted(p for p in child.iterdir() if p.is_dir())
                    chosen = sizes if policy.get("all_models") else [p for p in sizes if _under(p, models)] or sizes
                    for size in chosen:
                        walk(size, "model")
                elif child.name == "versions":
                    for version in sorted(p for p in child.iterdir() if p.is_dir()):
                        files = [p for p in _iter_files(version)]
                        wanted = any(os.path.normcase(os.path.abspath(str(p))) in models for p in files)
                        if policy["native"] and (wanted or policy["engines"]):
                            walk(version, "model")
                elif child.name == "discovered":
                    if policy["discovered"]:
                        walk(child, "model")
                elif policy["engines"]:
                    walk(child, "model")
        elif name == "runtime":
            for child in sorted(p for p in path.iterdir()):
                if not child.is_dir():
                    continue
                if child.name == "qwen-batch":
                    for part in sorted(p for p in child.iterdir()):
                        if part.is_dir() and f"runtime/qwen-batch/{part.name}" not in SKIP_RUNTIME:
                            walk(part, "runtime")
                elif child.name == "prompt-cache":
                    walk(child, "runtime")
                elif child.name.startswith("v"):
                    files = list(_iter_files(child))
                    wanted = any(os.path.normcase(os.path.abspath(str(p))) in models for p in files)
                    if policy["native"] and (wanted or policy["engines"]):
                        walk(child, "runtime")
    return entries


def summarize(entries) -> dict:
    groups = {}
    for entry in entries:
        slot = groups.setdefault(entry["group"], {"files": 0, "bytes": 0})
        slot["files"] += 1
        slot["bytes"] += entry["size"]
    total = sum(e["size"] for e in entries)
    return {"files": len(entries), "bytes": total, "groups": groups}


def build(data_dir, scope: str = "batch", install_dir=None, snapshot: Path | None = None,
          version: str = "", cache_only: bool = False) -> dict:
    """Return a JSON-ready manifest. Set cache_only to skip hashing (fast preview)."""
    if scope not in SCOPES:
        raise ValueError(f"未知的交接范围：{scope}")
    data_dir = Path(data_dir)
    cache_path = data_dir / "handoff" / CACHE_NAME
    cache = None if cache_only else _load_cache(cache_path)
    started = time.time()
    entries = _program_entries(Path(install_dir) if install_dir else None, cache)
    entries += _data_entries(data_dir, scope, cache, snapshot)
    if not cache_only:
        atomic_json(cache_path, cache)
    return {"schema": SCHEMA, "version": version, "scope": scope, "scope_label": SCOPES[scope]["label"],
            "created": started, "data_dir": str(data_dir),
            "install_dir": str(Path(install_dir).resolve()) if install_dir else "",
            "digest_ms": int((time.time() - started) * 1000),
            **summarize(entries), "entries": entries}


class Builder:
    """Serialize manifest builds; hashing 18 GB is not something to run twice at once."""

    def __init__(self, data_dir, install_dir=None, version=""):
        self.data_dir = Path(data_dir)
        self.install_dir = install_dir
        self.version = version
        self.lock = threading.Lock()

    def build(self, scope="batch", snapshot=None, cache_only=False):
        with self.lock:
            return build(self.data_dir, scope, self.install_dir, snapshot, self.version, cache_only)
