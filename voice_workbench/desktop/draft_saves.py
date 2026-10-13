"""Per-task draft snapshots, independent of the currently visible Qt editor."""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from time import time_ns

from ..config import atomic_json


@dataclass(frozen=True)
class DraftSnapshot:
    task_id: str
    revision: int
    body: dict


class DraftSaveQueue:
    def __init__(self, path: Path, title_field="title"):
        self.path = Path(path)
        self.title_field = title_field
        self.pending = {}
        self.revision = 0
        self.load_error = None
        self.write_error = None
        if self.path.is_file():
            try:
                items = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    raise ValueError("待存草稿应为列表")
                snapshots = [DraftSnapshot(**item) for item in items]
                if any(not isinstance(s.task_id, str) or not isinstance(s.revision, int)
                       or not isinstance(s.body, dict) or not isinstance(s.body.get(self.title_field), str) for s in snapshots):
                    raise ValueError("待存草稿格式无效")
                self.pending = {s.task_id: s for s in snapshots}
                self.revision = max((s.revision for s in snapshots), default=0)
            except (OSError, ValueError, TypeError) as exc:
                self.load_error = str(exc)

    def _persist(self):
        try:
            if self.load_error and self.path.exists():
                prefix = "pending-drafts" if self.title_field == "title" else self.path.stem
                self.path.replace(self.path.with_name(f"{prefix}-unreadable-{time_ns()}.json"))
                self.load_error = None
            atomic_json(self.path, [vars(s) for s in self.pending.values()])
            self.write_error = None
        except OSError as exc:
            # Keep the in-memory snapshots and still attempt the HTTP save.
            self.write_error = str(exc)

    def stage(self, task_id, body):
        existing = self.pending.get(task_id)
        if existing and existing.body == body:
            if self.write_error:
                self._persist()
            return existing
        self.revision += 1
        snapshot = DraftSnapshot(task_id, self.revision, deepcopy(body))
        self.pending[task_id] = snapshot
        self._persist()
        return snapshot

    def acknowledge(self, snapshot):
        latest = self.pending.get(snapshot.task_id)
        if latest and latest.revision <= snapshot.revision:
            del self.pending[snapshot.task_id]
            self._persist()

    def discard(self, task_id):
        if self.pending.pop(task_id, None) is not None:
            self._persist()

    def retry_later(self, snapshot):
        latest = self.pending.pop(snapshot.task_id, None)
        if latest:
            self.pending[snapshot.task_id] = latest
