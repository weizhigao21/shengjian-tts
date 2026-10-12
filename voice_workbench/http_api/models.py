"""Models HTTP responsibilities."""
from __future__ import annotations
from fastapi import Request, APIRouter
from ..schemas import ModelDownload, ModelRemoval, ModelImport, EngineDownload
from .context import manager

router = APIRouter()


@router.get("/models")
def models(request: Request):
    return manager(request).models.listing()


@router.post("/models/import", status_code=201)
def import_model(body: ModelImport, request: Request):
    return manager(request).models.register(body.path)


@router.post("/models/check-updates")
def check_model_updates(body: ModelDownload, request: Request):
    return manager(request).models.check_updates(body.source)


@router.post("/models/{model_id}/update", status_code=202)
def update_model(model_id: str, body: ModelDownload, request: Request):
    return manager(request).models.start_update(model_id, body.source)


@router.put("/models/runtime")
def runtime(body: ModelImport, request: Request):
    return manager(request).models.set_runtime(body.path)


@router.post("/models/runtime/download", status_code=202)
def download_runtime(body: EngineDownload, request: Request):
    return manager(request).models.engines.start(body.backend)


@router.post("/models/runtime/pause")
def pause_runtime(body: EngineDownload, request: Request):
    return manager(request).models.engines.pause(body.backend)


@router.post("/models/runtime/use")
def use_runtime(body: EngineDownload, request: Request):
    return manager(request).models.engines.use(body.backend)


@router.post("/models/runtime/verify")
def verify_runtime(body: EngineDownload, request: Request):
    return manager(request).models.engines.verify(body.backend)


@router.post("/models/runtime/check-updates")
def check_runtime_updates(request: Request):
    return manager(request).models.engines.check_updates()


@router.post("/models/runtime/upgrade", status_code=202)
def upgrade_runtime(body: EngineDownload, request: Request):
    return manager(request).models.engines.start(body.backend, upgrade=True)


@router.post("/models/runtime/rollback")
def rollback_runtime(body: EngineDownload, request: Request):
    return manager(request).models.engines.rollback(body.backend)


@router.post("/models/{model_id}/download", status_code=202)
def download_model(model_id: str, body: ModelDownload, request: Request):
    return manager(request).models.start_download(model_id, body.source)


@router.post("/models/{model_id}/pause")
def pause_download(model_id: str, request: Request):
    return manager(request).models.pause(model_id)


@router.post("/models/{model_id}/rollback")
def rollback_model(model_id: str, request: Request):
    return manager(request).models.rollback(model_id)


@router.post("/models/{model_id}/remove")
def remove_model(model_id: str, body: ModelRemoval, request: Request):
    return manager(request).models.remove(model_id, body.delete_file)
