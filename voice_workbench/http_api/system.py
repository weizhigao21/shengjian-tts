"""System HTTP responsibilities."""
from __future__ import annotations
from fastapi import Request, APIRouter
from .. import __version__
from ..errors import VoiceError
from ..schemas import PerformanceSettings
from .context import manager

router = APIRouter()


@router.get("/")
@router.get("/health")
def health(request: Request):
    return {"service": "local-voice-workbench", "version": __version__, "capabilities": ["async_jobs", "legacy_infer", "reference_library", "task_delete", "generation_options", "work_generations", "asset_management", "voice_discovery", "workflow_grouping", "workflow_parallel", "workflow_record_only", "engine_download", "engine_verification", "engine_updates", "model_updates", "script_voice_mentions", "dialogue_editor", "dialogue_audio_scope", "fixed_creation_type", "index_emotion_controls", "dialogue_role_names", "audio_editor"],
            "status": "running", "data_dir": str(manager(request).root), "engine": manager(request).models.engines.listing(), **manager(request).status()}


@router.get("/settings/performance")
def performance(request: Request):
    m = manager(request)
    return {**m.config.performance(), "notice": m.performance_notice}


@router.put("/settings/performance")
def set_performance(body: PerformanceSettings, request: Request):
    return manager(request).set_performance(body.model_dump())


@router.post("/engine/release")
def release(request: Request):
    m = manager(request)
    with m.lock:
        if m.current or m.editing_busy:
            raise VoiceError("ENGINE_BUSY", "当前正在生成，请先取消该任务", stage="释放引擎")
        m.release_runners()
    return {"msg": "推理进程已停止，显存已释放；下次任务会重新加载模型。"}


@router.post("/shutdown")
def shutdown(request: Request):
    server = getattr(request.app.state, "server", None)
    if server is not None:
        server.should_exit = True
    return {"msg": "服务正在停止，已完成片段保留，未完成片段下次启动恢复。"}
