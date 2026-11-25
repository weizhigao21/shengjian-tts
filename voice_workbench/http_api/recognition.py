"""Local speech recognition routes; audio is imported by path and served back for playback."""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..errors import VoiceError
from ..speech_recognition.schemas import (BatchRequest, LineCorrections, RecognizeRequest,
                                           SelectionRequest, SessionState)
from ..translation.schemas import TranslationRequest
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


@router.get("/batches")
def batches(request: Request):
    return manager(request).recognition.batching.recent()


@router.post("/batches", status_code=201)
def create_batch(body: BatchRequest, request: Request):
    return manager(request).recognition.batching.start(body.paths, body.language, body.format, body.name,
                                                       body.dedupe)


@router.get("/batches/{identity}")
def batch(identity: str, request: Request):
    return manager(request).recognition.batching.state(identity)


@router.post("/batches/{identity}/cancel")
def cancel_batch(identity: str, request: Request):
    return manager(request).recognition.batching.cancel(identity)


@router.post("/batches/{identity}/retry")
def retry_batch(identity: str, request: Request):
    return manager(request).recognition.batching.retry(identity)


@router.delete("/batches/{identity}")
def remove_batch(identity: str, request: Request):
    return manager(request).recognition.batching.remove(identity)


@router.post("/sessions", status_code=201)
def create(body: RecognizeRequest, request: Request):
    return manager(request).recognition.import_audio(body.path, body.name, body.language, dedupe=body.dedupe)


@router.get("/sessions/{identity}")
def session(identity: str, request: Request):
    return manager(request).recognition.session(identity)


@router.patch("/sessions/{identity}")
def save(identity: str, body: SessionState, request: Request):
    recognition = manager(request).recognition
    recognition.store.update(identity, {"name": body.name, "language": body.language})
    recognition.choose(language=body.language)
    return recognition.session(identity)


@router.patch("/sessions/{identity}/lines")
def correct_lines(identity: str, body: LineCorrections, request: Request):
    return manager(request).recognition.correct_lines(identity, body.corrections, origin="manual")


@router.post("/sessions/{identity}/retry")
def retry(identity: str, request: Request):
    return manager(request).recognition.retry(identity)


@router.post("/sessions/{identity}/translation")
def translate(identity: str, body: TranslationRequest, request: Request):
    return manager(request).translation.translate_session(identity, body.target, body.provider)


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
