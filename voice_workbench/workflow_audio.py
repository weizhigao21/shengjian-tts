"""Temporary workflow delivery, isolated from the desktop's persistent cache."""
from collections import defaultdict
from pathlib import Path
import re

from .errors import VoiceError


class WorkflowAudio:
    def __init__(self, manager):
        self.manager = manager
        self.root = manager.root / "workflow-audio"
        self.root.mkdir(exist_ok=True)

    def path(self, record):
        filename = record["audio"]
        if not re.fullmatch(r"[0-9a-f]{64}\.wav", filename or ""):
            raise VoiceError("WORKFLOW_AUDIO_NOT_FOUND", "临时音频不存在", stage="工作流音频")
        folder = self.root if record["audio_storage"] == "temporary" else self.manager.root / "audio"
        path = folder / filename
        if path.resolve().parent != folder.resolve() or path.is_symlink():
            raise VoiceError("WORKFLOW_AUDIO_NOT_FOUND", "音频路径超出管理目录", stage="工作流音频")
        return path

    def lookup(self, filename):
        with self.manager.lock:
            records = self.manager.store.workflow_audio_records(filename=filename)
            if not records:
                raise VoiceError("WORKFLOW_AUDIO_NOT_FOUND", "临时音频已清理或不存在", stage="工作流音频")
            record = records[0]
            path = self.path(record)
            if not path.is_file():
                raise VoiceError("WORKFLOW_AUDIO_NOT_FOUND", "临时音频已清理或不存在", stage="工作流音频")
            return path, bool(record["delivery_ack"])

    def release(self, records, reason):
        """Keep all snapshots; unlink a file only after its last owning record."""
        with self.manager.lock:
            if not records:
                return {"released": [], "errors": []}
            references = self._reference_paths()
            groups = defaultdict(list)
            for record in records:
                groups[(record["audio"], record["audio_storage"])].append(record)
            released, errors, completed, caches = [], [], [], []
            for (filename, storage), selected in groups.items():
                ids = {f"{r['job_id']}:{r['idx']}" for r in selected}
                remaining = self.manager.store.audio_references(filename, storage) - ids
                try:
                    path = self.path(selected[0])
                    if not remaining and path.resolve() not in references:
                        path.unlink(missing_ok=True)
                        if storage == "permanent":
                            caches.append(filename)
                except (OSError, VoiceError) as exc:
                    errors.append(f"{filename}：{exc}")
                    continue
                completed.extend(selected)
                released.extend(sorted(ids))
            # One transaction for a historical batch; per-file commits made large
            # libraries stall and repeated the same reference-path parsing.
            self.manager.store.release_workflow_audio(completed, reason, caches)
            return {"released": released, "errors": errors}

    def _reference_paths(self):
        # Legacy configurations sometimes used a generated clip directly as a
        # cloning reference. Preserve that asset even after its workflow record.
        pending = [self.manager.config.read()]
        raw = set()
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for field in ("reference_audio", "emotion_reference_audio"):
                    reference = value.get(field)
                    if isinstance(reference, str) and reference:
                        raw.add(reference)
                pending.extend(v for v in value.values() if isinstance(v, (dict, list)))
            elif isinstance(value, list):
                pending.extend(value)
        return {Path(value).resolve() for value in raw} | self.manager.store.reference_audio_paths()

    def delivered(self, filename):
        # Idempotent so a lost acknowledgement can be retried without regeneration.
        return self.release(self.manager.store.workflow_audio_records(filename=filename), "delivered")

    def expired(self):
        return self.release(self.manager.store.workflow_audio_records(expired=True), "expired")

    def cancelled(self, job_id, generation=None):
        records = self.manager.store.workflow_audio_records(job_id=job_id, generation=generation)
        return self.release(records, "cancelled")

    def cleanup_all(self):
        with self.manager.lock:
            if any(j["record_only"] and j["status"] in ("queued", "running")
                   for j in self.manager.store.list(100000, include_workflow=True)):
                raise VoiceError("ENGINE_BUSY", "工作流仍在生成，请完成或停止后清理", stage="工作流音频清理")
            return self.release(self.manager.store.workflow_audio_records(), "history_cleanup")
