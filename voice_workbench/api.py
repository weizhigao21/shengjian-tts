from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import __version__
from .config import DEFAULT_DATA
from .errors import VoiceError
from .manager import Manager
from .schemas import JobInput, DocumentInput, LegacyInput, VoiceConfig, RenameJob, ReferenceInput, ReferenceEdit
from .schemas import AudioDeletion, ModelDownload, ModelRemoval, ModelImport
from .schemas import PerformanceSettings
from .workflow import voice_records, workflow_manifest


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
        return JSONResponse(status_code=404 if exc.code in ("JOB_NOT_FOUND", "VOICE_NOT_FOUND", "REFERENCE_NOT_FOUND") else 400,
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

    def manager(request):
        return request.app.state.manager

    def get_job(m, job_id, generation=None):
        job = m.store.get(job_id, generation=generation)
        if job is None:
            raise VoiceError("JOB_NOT_FOUND", "任务不存在", stage="任务查询")
        for item in job["segments"]:
            item["audio_url"] = f"/audio/{item['audio']}" if item["audio"] else None
        return job

    @app.get("/")
    @app.get("/health")
    def health(request: Request):
        return {"service": "local-voice-workbench", "version": __version__, "capabilities": ["async_jobs", "legacy_infer", "reference_library", "task_delete", "generation_options", "work_generations", "asset_management", "voice_discovery", "workflow_grouping", "workflow_parallel"],
                "status": "running", "data_dir": str(manager(request).root), **manager(request).status()}

    @app.get("/workflow")
    def workflow(request: Request):
        return workflow_manifest(manager(request).config, str(request.base_url))

    @app.get("/models")
    def models(request: Request):
        return manager(request).models.listing()

    @app.post("/models/import", status_code=201)
    def import_model(body: ModelImport, request: Request):
        return manager(request).models.register(body.path)

    @app.put("/models/runtime")
    def runtime(body: ModelImport, request: Request):
        return manager(request).models.set_runtime(body.path)

    @app.post("/models/{model_id}/download", status_code=202)
    def download_model(model_id: str, body: ModelDownload, request: Request):
        return manager(request).models.start_download(model_id, body.source)

    @app.post("/models/{model_id}/pause")
    def pause_download(model_id: str, request: Request):
        return manager(request).models.pause(model_id)

    @app.post("/models/{model_id}/remove")
    def remove_model(model_id: str, body: ModelRemoval, request: Request):
        return manager(request).models.remove(model_id, body.delete_file)

    @app.get("/generated-audio")
    def generated_audio(request: Request):
        return manager(request).generated_audio()

    @app.post("/generated-audio/delete")
    def delete_generated_audio(body: AudioDeletion, request: Request):
        return manager(request).delete_generated_audio(body.ids, body.all)

    @app.get("/voices")
    def voices(request: Request):
        return voice_records(manager(request).config)

    @app.put("/voices/{voice_id}")
    def save_voice(voice_id: str, body: VoiceConfig, request: Request):
        if voice_id != body.id:
            raise VoiceError("VOICE_ID_MISMATCH", "路径中的声音 ID 与配置不一致", stage="配置保存")
        return manager(request).config.save_voice(body.model_dump())

    @app.post("/import/yovoice")
    def import_yovoice(request: Request):
        return {"added": manager(request).config.import_yovoice()}

    @app.delete("/voices/{voice_id}")
    def delete_voice(voice_id: str, request: Request):
        manager(request).config.delete_voice(voice_id)
        return {"deleted": voice_id}

    @app.post("/voices/{voice_id}/restore")
    def restore_voice(voice_id: str, request: Request):
        return manager(request).config.restore_voice(voice_id)

    @app.get("/references")
    def references(request: Request):
        return manager(request).config.references()

    @app.post("/references", status_code=201)
    def import_reference(body: ReferenceInput, request: Request):
        return manager(request).config.import_reference(body.model_dump())

    @app.patch("/references/{ref_id}")
    def edit_reference(ref_id: str, body: ReferenceEdit, request: Request):
        return manager(request).config.edit_reference(ref_id, body.model_dump())

    @app.delete("/references/{ref_id}")
    def delete_reference(ref_id: str, request: Request):
        manager(request).config.delete_reference(ref_id)
        return {"deleted": ref_id}

    @app.post("/references/{ref_id}/restore")
    def restore_reference(ref_id: str, request: Request):
        return manager(request).config.restore_reference(ref_id)

    @app.get("/jobs")
    def jobs(request: Request):
        return manager(request).store.list()

    @app.post("/jobs", status_code=202)
    def submit(body: JobInput, request: Request):
        m = manager(request)
        with m.lock:
            job_id = m.submit(body.title, body.voice_id, [s.model_dump() for s in body.segments],
                              overrides=body.overrides.model_dump(exclude_none=True) if body.overrides else None,
                              script=body.script, workflow_key=body.workflow_key)
            return submitted(m, job_id)

    def submitted(m, job_id):
        return m.store.submission(job_id)

    @app.post("/jobs/draft", status_code=201)
    def create_draft(body: DocumentInput, request: Request):
        job_id = manager(request).store.create(body.title, {}, "", [], body.script)
        return get_job(manager(request), job_id)

    @app.post("/jobs/{job_id}/generate", status_code=202)
    def append_generation(job_id: str, body: JobInput, request: Request):
        m = manager(request)
        with m.lock:
            existing = get_job(m, job_id)
            title = body.title if "title" in body.model_fields_set else existing["title"]
            m.submit(title, body.voice_id, [s.model_dump() for s in body.segments],
                     overrides=body.overrides.model_dump(exclude_none=True) if body.overrides else None,
                     job_id=job_id, script=body.script)
            return submitted(m, job_id)

    @app.patch("/jobs/{job_id}/document")
    def save_document(job_id: str, body: DocumentInput, request: Request):
        m = manager(request)
        get_job(m, job_id)
        title = m.store.save_document(job_id, body.title, body.script)
        return {"saved": job_id, "title": title}

    @app.get("/jobs/{job_id}")
    def job(job_id: str, request: Request, generation: int | None = None):
        result = get_job(manager(request), job_id, generation)
        if generation is not None:
            if not result["segments"]:
                raise VoiceError("JOB_NOT_FOUND", "生成批次不存在", stage="批次查询")
        return result

    @app.delete("/jobs/{job_id}")
    def delete_job(job_id: str, request: Request):
        manager(request).delete(job_id)
        return {"deleted": job_id}

    @app.post("/jobs/{job_id}/restore")
    def restore_job(job_id: str, request: Request):
        manager(request).restore(job_id)
        return get_job(manager(request), job_id)

    @app.patch("/jobs/{job_id}")
    def rename_job(job_id: str, body: RenameJob, request: Request):
        m = manager(request)
        get_job(m, job_id)
        if not body.title.strip():
            raise VoiceError("EMPTY_TITLE", "任务名称不能为空", stage="任务重命名")
        m.store.rename(job_id, body.title.strip())
        return get_job(m, job_id)

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        m = manager(request)
        get_job(m, job_id)
        m.cancel(job_id)
        return get_job(m, job_id)

    @app.post("/jobs/{job_id}/retry")
    def retry(job_id: str, request: Request):
        m = manager(request)
        get_job(m, job_id)
        m.retry(job_id)
        return get_job(m, job_id)

    @app.post("/jobs/{job_id}/generations/{generation}/cancel")
    def cancel_generation(job_id: str, generation: int, request: Request):
        m = manager(request)
        job(job_id, request, generation)
        m.cancel(job_id, generation)
        return job(job_id, request, generation)

    @app.get("/settings/performance")
    def performance(request: Request):
        m = manager(request)
        return {**m.config.performance(), "notice": m.performance_notice}

    @app.put("/settings/performance")
    def set_performance(body: PerformanceSettings, request: Request):
        return manager(request).set_performance(body.model_dump())

    @app.post("/engine/release")
    def release(request: Request):
        m = manager(request)
        with m.lock:
            if m.current:
                raise VoiceError("ENGINE_BUSY", "当前正在生成，请先取消该任务", stage="释放引擎")
            m.release_runners()
        return {"msg": "推理进程已停止，显存已释放；下次任务会重新加载模型。"}

    @app.post("/shutdown")
    def shutdown(request: Request):
        server = getattr(request.app.state, "server", None)
        if server is not None:
            server.should_exit = True
        return {"msg": "服务正在停止，已完成片段保留，未完成片段下次启动恢复。"}

    @app.get("/audio/{filename}")
    def audio(filename: str, request: Request):
        import re
        if not re.fullmatch(r"[0-9a-f]{64}\.wav", filename):
            return JSONResponse(status_code=404, content={"msg": "音频不存在"})
        path = manager(request).root / "audio" / filename
        if not path.is_file():
            return JSONResponse(status_code=404, content={"msg": "音频不存在"})
        return FileResponse(path, media_type="audio/wav")

    @app.get("/logs/{job_id}/{index}")
    def log(job_id: str, index: int, request: Request):
        import re
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return PlainTextResponse("日志不存在", status_code=404)
        path = manager(request).root / "logs" / f"{job_id}-{index}.json"
        return PlainTextResponse(path.read_text(encoding="utf-8") if path.is_file() else "该片段没有错误日志")

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
                # 不丢弃已生成音频；任务可从管理界面查看和恢复。
                return JSONResponse(status_code=202, content={"job_id": job_id, "msg": "任务仍在服务队列中"})
            await asyncio.sleep(0.1)
        else:
            return {"job_id": job_id, "msg": "任务仍在生成或排队；请在管理界面查看。新工作流适配器使用轮询，不受兼容接口等待时间限制。"}
        if job["status"] != "done":
            item = next((s for s in job["segments"] if s["error"]), None)
            error = item["error"] if item else {"message": "任务已取消"}
            return {"job_id": job_id, "msg": f"第 {item['idx']} 段：{error['message']}" if item else error["message"], "error": error}
        base = str(request.base_url).rstrip("/")
        urls = [base + s["audio_url"] for s in job["segments"]]
        return {"job_id": job_id, "audio_urls": urls} if bulk else {"job_id": job_id, "audio_url": urls[0]}

    @app.post("/infer_single")
    async def infer_single(body: LegacyInput, request: Request):
        return await legacy(body, request, False)

    @app.post("/infer_bulk")
    async def infer_bulk(body: LegacyInput, request: Request):
        return await legacy(body, request, True)

    return app
