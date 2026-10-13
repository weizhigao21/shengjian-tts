"""Local audio editor API; generated audio uses managed, validated filenames."""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from ..audio_editing.schemas import AudioImport, AudioOperation, EditState, EditModelImport
from ..errors import VoiceError
from .context import manager

router = APIRouter(prefix="/audio-editor")


@router.get("/options")
def options(request: Request):
    return manager(request).audio_editor.options()


@router.post("/models", status_code=201)
def import_model(body: EditModelImport, request: Request):
    return manager(request).audio_editor.import_model(body.path)


@router.get("/projects")
def projects(request: Request):
    return manager(request).audio_editor.store.recent()


@router.post("/projects", status_code=201)
def import_audio(body: AudioImport, request: Request):
    return manager(request).audio_editor.import_audio(body.path, body.name, body.original_text)


@router.get("/projects/{identity}")
def project(identity: str, request: Request):
    return manager(request).audio_editor.project(identity)


@router.patch("/projects/{identity}")
def save_project(identity: str, body: EditState, request: Request):
    editor = manager(request).audio_editor
    editor.store.save(identity, body.model_dump())
    return editor.project(identity)


@router.post("/projects/{identity}/process", status_code=202)
def process(identity: str, body: AudioOperation, request: Request):
    return manager(request).audio_editor.submit(identity, body.model_dump())


@router.post("/projects/{identity}/cancel")
def cancel(identity: str, request: Request):
    return manager(request).audio_editor.cancel(identity)


@router.get("/audio/{filename}")
def audio(filename: str, request: Request):
    editor = manager(request).audio_editor
    path = editor.audio_path(filename)
    if not path.is_file() or not editor.store.has_audio(filename):
        raise VoiceError("EDIT_AUDIO_NOT_FOUND", "音频编辑文件不存在")
    return FileResponse(path, media_type="audio/wav")
