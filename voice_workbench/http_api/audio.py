"""Audio HTTP responsibilities."""
from __future__ import annotations
from fastapi import Request, APIRouter
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from ..schemas import AudioDeletion
from .context import manager

router = APIRouter()


@router.get("/generated-audio")
def generated_audio(request: Request):
    return manager(request).generated_audio()


@router.post("/generated-audio/delete")
def delete_generated_audio(body: AudioDeletion, request: Request):
    return manager(request).delete_generated_audio(body.ids, body.all)


@router.get("/audio/{filename}")
def audio(filename: str, request: Request):
    import re
    if not re.fullmatch(r"[0-9a-f]{64}\.wav", filename):
        return JSONResponse(status_code=404, content={"msg": "音频不存在"})
    path = manager(request).root / "audio" / filename
    if not path.is_file():
        return JSONResponse(status_code=404, content={"msg": "音频不存在"})
    return FileResponse(path, media_type="audio/wav")


@router.get("/logs/{job_id}/{index}")
def log(job_id: str, index: int, request: Request):
    import re
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return PlainTextResponse("日志不存在", status_code=404)
    path = manager(request).root / "logs" / f"{job_id}-{index}.json"
    return PlainTextResponse(path.read_text(encoding="utf-8") if path.is_file() else "该片段没有错误日志")
