"""Jobs HTTP responsibilities."""
from __future__ import annotations
from fastapi import Request, APIRouter
from ..errors import VoiceError
from ..schemas import JobInput, DocumentInput, RenameJob
from .context import manager, get_job

router = APIRouter()


@router.get("/jobs")
def jobs(request: Request):
    return manager(request).store.list()


@router.post("/jobs", status_code=202)
def submit(body: JobInput, request: Request):
    m = manager(request)
    with m.lock:
        job_id = m.submit(body.title, body.voice_id, [s.model_dump() for s in body.segments],
                          overrides=body.overrides.model_dump(exclude_none=True) if body.overrides else None,
                          script=body.script, workflow_key=body.workflow_key, use_preset=body.use_preset,
                          record_only=body.record_only, delivery_ack=body.delivery_ack)
        return submitted(m, job_id)


def submitted(m, job_id):
    return m.store.submission(job_id)


@router.post("/jobs/draft", status_code=201)
def create_draft(body: DocumentInput, request: Request):
    job_id = manager(request).store.create(body.title, {}, "", [], body.script)
    return get_job(manager(request), job_id)


@router.post("/jobs/{job_id}/generate", status_code=202)
def append_generation(job_id: str, body: JobInput, request: Request):
    m = manager(request)
    with m.lock:
        existing = get_job(m, job_id)
        title = body.title if "title" in body.model_fields_set else existing["title"]
        m.submit(title, body.voice_id, [s.model_dump() for s in body.segments],
                 overrides=body.overrides.model_dump(exclude_none=True) if body.overrides else None,
                 job_id=job_id, script=body.script, use_preset=body.use_preset,
                 record_only=body.record_only, delivery_ack=body.delivery_ack)
        return submitted(m, job_id)


@router.patch("/jobs/{job_id}/document")
def save_document(job_id: str, body: DocumentInput, request: Request):
    m = manager(request)
    get_job(m, job_id)
    title = m.store.save_document(job_id, body.title, body.script)
    return {"saved": job_id, "title": title}


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request, generation: int | None = None):
    result = get_job(manager(request), job_id, generation)
    if generation is not None:
        if not result["segments"]:
            raise VoiceError("JOB_NOT_FOUND", "生成批次不存在", stage="批次查询")
    return result


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    manager(request).delete(job_id)
    return {"deleted": job_id}


@router.post("/jobs/{job_id}/restore")
def restore_job(job_id: str, request: Request):
    manager(request).restore(job_id)
    return get_job(manager(request), job_id)


@router.patch("/jobs/{job_id}")
def rename_job(job_id: str, body: RenameJob, request: Request):
    m = manager(request)
    get_job(m, job_id)
    if not body.title.strip():
        raise VoiceError("EMPTY_TITLE", "任务名称不能为空", stage="任务重命名")
    m.store.rename(job_id, body.title.strip())
    return get_job(m, job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request):
    m = manager(request)
    get_job(m, job_id)
    m.cancel(job_id)
    return get_job(m, job_id)


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str, request: Request):
    m = manager(request)
    get_job(m, job_id)
    m.retry(job_id)
    return get_job(m, job_id)


@router.post("/jobs/{job_id}/generations/{generation}/cancel")
def cancel_generation(job_id: str, generation: int, request: Request):
    m = manager(request)
    job(job_id, request, generation)
    m.cancel(job_id, generation)
    return job(job_id, request, generation)
