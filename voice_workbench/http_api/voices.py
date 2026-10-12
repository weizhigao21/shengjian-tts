"""Voices HTTP responsibilities."""
from __future__ import annotations
from fastapi import Request, APIRouter
from ..errors import VoiceError
from ..schemas import VoiceConfig, ReferenceInput, ReferenceEdit
from ..workflow import voice_records
from .context import manager

router = APIRouter()


@router.get("/voices")
def voices(request: Request):
    return voice_records(manager(request).config)


@router.put("/voices/{voice_id}")
def save_voice(voice_id: str, body: VoiceConfig, request: Request):
    if voice_id != body.id:
        raise VoiceError("VOICE_ID_MISMATCH", "路径中的声音 ID 与配置不一致", stage="配置保存")
    return manager(request).config.save_voice(body.model_dump())


@router.post("/import/yovoice")
def import_yovoice(request: Request):
    return {"added": manager(request).config.import_yovoice()}


@router.delete("/voices/{voice_id}")
def delete_voice(voice_id: str, request: Request):
    manager(request).config.delete_voice(voice_id)
    return {"deleted": voice_id}


@router.post("/voices/{voice_id}/restore")
def restore_voice(voice_id: str, request: Request):
    return manager(request).config.restore_voice(voice_id)


@router.get("/references")
def references(request: Request):
    return manager(request).config.references()


@router.post("/references", status_code=201)
def import_reference(body: ReferenceInput, request: Request):
    return manager(request).config.import_reference(body.model_dump())


@router.patch("/references/{ref_id}")
def edit_reference(ref_id: str, body: ReferenceEdit, request: Request):
    return manager(request).config.edit_reference(ref_id, body.model_dump())


@router.delete("/references/{ref_id}")
def delete_reference(ref_id: str, request: Request):
    manager(request).config.delete_reference(ref_id)
    return {"deleted": ref_id}


@router.post("/references/{ref_id}/restore")
def restore_reference(ref_id: str, request: Request):
    return manager(request).config.restore_reference(ref_id)
