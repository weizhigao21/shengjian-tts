"""Discover/configure optional batch backends and preview routing without queuing."""
from fastapi import APIRouter, Request
from ..batch_inference.schemas import BatchSettings, PlanRequest, SetupRequest
from ..errors import VoiceError
from .context import manager

router = APIRouter()


@router.get('/inference/backends')
def backends(request: Request):
    status = manager(request).batch.status()
    return {'default': 'auto', 'backends': [{'id': 'native', 'ready': True, 'batch': False},
            {'id': 'qwen3_batch', 'ready': status['ready'], 'batch': True, 'reason': status['reason'],
             'loaded': status['loaded'], 'family': 'qwen3_tts', 'mode': 'clone'}]}


@router.get('/settings/batch-inference')
def settings(request: Request):
    batch = manager(request).batch
    batch.setup.ensure_scan()
    return {**batch.status(), 'setup': batch.setup.options()}


@router.put('/settings/batch-inference')
def configure(body: BatchSettings, request: Request):
    return manager(request).batch.configure(body)


@router.post('/inference/setup', status_code=202)
def setup(body: SetupRequest, request: Request):
    return manager(request).batch.setup.start(**body.model_dump())


@router.post('/inference/setup/pause')
def pause_setup(request: Request):
    return manager(request).batch.setup.pause()


@router.post('/inference/plan')
def plan(body: PlanRequest, request: Request):
    if any(not text.strip() or len(text) > 5000 for text in body.texts):
        raise VoiceError('INVALID_TEXT', '每条文本需为 1–5000 字', stage='批量计划')
    m = manager(request)
    voice, _ = m.config.resolve(body.voice_id)
    # Resolve only. No seed materialization, reference copy, queued job or model load.
    return m.batch.plan(voice, body.texts)
