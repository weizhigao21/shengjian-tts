"""Legacy HTTP responsibilities."""
from __future__ import annotations
import asyncio
import time
from fastapi import Request, APIRouter
from fastapi.responses import JSONResponse
from ..errors import VoiceError
from ..schemas import LegacyInput
from .context import manager, get_job

router = APIRouter()


async def legacy(body, request, bulk):
    texts = body.texts if bulk else [body.text]
    if not texts or len(texts) > 1000 or any(not isinstance(t, str) or not t.strip() or len(t) > 5000 for t in texts):
        raise VoiceError("INVALID_TEXT", "请提供有效的 text / texts，每段最多 5000 字", stage="提交")
    overrides = {}
    if body.speed_facter is not None:
        overrides["speed"] = body.speed_facter
    if body.emotion and body.emotion != "默认":
        overrides["emotion"] = body.emotion
    langs = {"中文": "zh", "英文": "en", "日语": "ja", "西班牙语": "es", "阿拉伯语": "ar"}
    if body.text_lang:
        language = langs.get(body.text_lang, body.text_lang.lower())
        if language not in ("zh", "en", "ja", "es", "ar"):
            raise VoiceError("UNSUPPORTED_LANGUAGE", f"不支持语言：{body.text_lang}", stage="配置检查")
        overrides["language"] = language
    m = manager(request)
    job_id = m.submit("工作流兼容请求", body.model_name, [{"text": t} for t in texts], overrides)
    deadline = time.monotonic() + (285 if bulk else 50)
    while time.monotonic() < deadline:
        job = get_job(m, job_id)
        if job["status"] in ("done", "failed", "cancelled"):
            break
        if await request.is_disconnected():
            # Keep pending clips for their download grace period, without a sidebar work.
            return JSONResponse(status_code=202, content={"job_id": job_id, "msg": "任务仍在服务队列中"})
        await asyncio.sleep(0.1)
    else:
        return {"job_id": job_id, "msg": "工作流仍在生成或排队；记录可通过接口查询。新版工作流适配器使用轮询，不受兼容接口等待时间限制。"}
    if job["status"] != "done":
        item = next((s for s in job["segments"] if s["error"]), None)
        error = item["error"] if item else {"message": "任务已取消"}
        return {"job_id": job_id, "msg": f"第 {item['idx']} 段：{error['message']}" if item else error["message"], "error": error}
    base = str(request.base_url).rstrip("/")
    urls = [base + s["audio_url"] for s in job["segments"]]
    return {"job_id": job_id, "audio_urls": urls} if bulk else {"job_id": job_id, "audio_url": urls[0]}


@router.post("/infer_single")
async def infer_single(body: LegacyInput, request: Request):
    return await legacy(body, request, False)


@router.post("/infer_bulk")
async def infer_bulk(body: LegacyInput, request: Request):
    return await legacy(body, request, True)
