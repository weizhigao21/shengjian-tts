"""主仆模式 HTTP 职责：开始/停止交接、查询进度、下发引导包。"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from urllib.parse import quote

from ..errors import VoiceError
from ..schemas import HandoffStart
from .context import manager

router = APIRouter()


@router.get("/handoff")
def handoff_status(request: Request):
    return manager(request).handoff.status()


@router.post("/handoff/start", status_code=202)
def handoff_start(body: HandoffStart, request: Request):
    return manager(request).handoff.start(body.scope)


@router.post("/handoff/stop")
def handoff_stop(request: Request):
    state = manager(request).handoff
    state.stop()
    return state.status()


@router.post("/handoff/pair/{token}/approve")
def handoff_pair_approve(token: str, request: Request):
    """主电脑点「允许」：配对码随后按令牌走 UDP 单发一次，用完即焚。"""
    return {"approved": manager(request).handoff.pairs.approve(token)}


@router.post("/handoff/pair/{token}/deny")
def handoff_pair_deny(token: str, request: Request):
    return {"denied": manager(request).handoff.pairs.deny(token)}


@router.get("/handoff/bootstrap")
def handoff_bootstrap(request: Request):
    """给用户下载的那个小压缩包；目标机解压后双击即可开始接收。"""
    from ..handoff.server import build_bootstrap_zip, lan_address

    state = manager(request).handoff
    status = state.status()
    if status["phase"] != "ready" or not state.manifest:
        raise VoiceError("HANDOFF_NOT_READY", "交接还没准备好", "等清单准备完成再下载引导包。", "局域网分发")
    with state.lock:
        payload = build_bootstrap_zip(lan_address(), state.port, state.code, state.scope, state.manifest)
    return Response(content=payload, media_type="application/zip", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''%s" % quote("声笺-局域网引导包.zip")})
