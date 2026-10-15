"""Local speech recognition routes; audio is imported by path and served back for playback."""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..errors import VoiceError
from ..speech_recognition.schemas import RecognizeRequest, SelectionRequest, SessionState
from .context import manager

router = APIRouter(prefix="/recognition")


@router.get("/options")
def options(request: Request):
    return manager(request).recognition.options()


@router.post("/selection")
def selection(body: SelectionRequest, request: Request):
    return manager(request).recognition.choose(asr_model=body.asr_model, align_model=body.align_model,
                                               language=body.language)


@router.get("/sessions")
def sessions(request: Request):
    return manager(request).recognition.store.recent()


@router.post("/sessions", status_code=201)
def create(body: RecognizeRequest, request: Request):
    return manager(request).recognition.import_audio(body.path, body.name, body.language)


@router.get("/sessions/{identity}")
def session(identity: str, request: Request):
    return manager(request).recognition.session(identity)


@router.patch("/sessions/{identity}")
def save(identity: str, body: SessionState, request: Request):
    recognition = manager(request).recognition
    recognition.store.update(identity, {"name": body.name, "language": body.language})
    recognition.choose(language=body.language)
    return recognition.session(identity)


@router.post("/sessions/{identity}/retry")
def retry(identity: str, request: Request):
    return manager(request).recognition.retry(identity)


@router.post("/sessions/{identity}/cancel")
def cancel(identity: str, request: Request):
    return manager(request).recognition.cancel(identity)


@router.delete("/sessions/{identity}")
def remove(identity: str, request: Request):
    return manager(request).recognition.remove(identity)


@router.get("/audio/{filename}")
def audio(filename: str, request: Request):
    recognition = manager(request).recognition
    path = recognition.audio_path(filename)
    if not path.is_file():
        raise VoiceError("RECOGNITION_AUDIO_NOT_FOUND", "识别音频不存在")
    return FileResponse(path)
