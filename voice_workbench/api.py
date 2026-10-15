"""FastAPI application assembly, lifespan and shared error/access handling."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from . import __version__
from .config import DEFAULT_DATA
from .errors import VoiceError
from .manager import Manager
from .http_api import ROUTERS


def create_app(data_dir: Path = DEFAULT_DATA, runner=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = Manager(data_dir, runner=runner)
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.manager.close)

    app = FastAPI(title="本地配音工作台", version=__version__, lifespan=lifespan)

    @app.exception_handler(VoiceError)
    async def voice_error(request, exc):
        return JSONResponse(status_code=404 if exc.code in ("JOB_NOT_FOUND", "VOICE_NOT_FOUND", "REFERENCE_NOT_FOUND", "WORKFLOW_AUDIO_NOT_FOUND", "EDIT_PROJECT_NOT_FOUND", "EDIT_AUDIO_NOT_FOUND", "PARAMETER_PRESET_NOT_FOUND", "RECOGNITION_NOT_FOUND", "RECOGNITION_AUDIO_NOT_FOUND") else 400,
                            content={"msg": exc.message, "error": exc.as_dict()})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(status_code=422, content={"msg": "请求参数无效", "errors": [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]})

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        # 只服务本机；阻止浏览器从第三方站点发起修改请求。
        host = request.headers.get("host", "").split(":")[0]
        origin = request.headers.get("origin")
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse(status_code=403, content={"msg": "服务仅接受本机访问"})
        if origin and origin not in (f"http://{request.headers.get('host')}",):
            return JSONResponse(status_code=403, content={"msg": "不接受跨站请求"})
        return await call_next(request)

    for router in ROUTERS:
        app.include_router(router)
    return app
