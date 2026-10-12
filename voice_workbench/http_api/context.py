"""Context HTTP responsibilities."""
from __future__ import annotations
from ..errors import VoiceError


def manager(request):
    return request.app.state.manager


def get_job(m, job_id, generation=None):
    job = m.store.get(job_id, generation=generation)
    if job is None:
        raise VoiceError("JOB_NOT_FOUND", "任务不存在", stage="任务查询")
    for item in job["segments"]:
        prefix = "/workflow/audio" if job["record_only"] else "/audio"
        item["audio_url"] = f"{prefix}/{item['audio']}" if item["audio"] else None
    return job
