"""Workflow HTTP responsibilities."""
from __future__ import annotations
from fastapi import Request, APIRouter
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from ..workflow import workflow_manifest
from .context import manager

router = APIRouter()


@router.get("/workflow")
def workflow(request: Request):
    return workflow_manifest(manager(request).config, str(request.base_url))


@router.get("/workflow/records")
def workflow_records(request: Request, limit: int = 200):
    return manager(request).store.workflow_records(max(1, min(limit, 500)))


@router.post("/workflow/records/cleanup")
def cleanup_workflow_records(request: Request):
    return manager(request).workflow_audio.cleanup_all()


@router.api_route("/workflow/audio/{filename}", methods=["GET", "HEAD"])
def workflow_audio(filename: str, request: Request):
    delivery = manager(request).workflow_audio
    path, acknowledged = delivery.lookup(filename)
    # Updated adapters acknowledge after an atomic save. Old adapters consume
    # only a complete GET; HEAD/range previews must never release the file.
    background = None
    if not acknowledged and request.method == "GET" and "range" not in request.headers:
        background = BackgroundTask(delivery.delivered, filename)
    return FileResponse(path, media_type="audio/wav", background=background,
                        headers={"Cache-Control": "no-store"})


@router.post("/workflow/audio/{filename}/release")
def release_workflow_audio(filename: str, request: Request):
    return manager(request).workflow_audio.delivered(filename)
