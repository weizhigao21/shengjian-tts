"""Translation providers, terminology, memory and the bounded translation agent."""
from fastapi import APIRouter, Request

from ..translation.schemas import (AgentChatRequest, AgentTranslateRequest, ApplyTranslationRequest,
                                   MemoryInput, MemoryUpdate, TermInput, TermUpdate, TranslationSettings)
from .context import manager

router = APIRouter(prefix="/translation")


@router.get("/options")
def options(request: Request):
    return manager(request).translation.options()


@router.post("/settings")
def settings(body: TranslationSettings, request: Request):
    return manager(request).translation.save_settings(body.model_dump(exclude_unset=True))


# --------------------------------------------------------------------- terms


@router.get("/terms")
def terms(request: Request, query: str = "", target: str = "", enabled_only: bool = True,
          limit: int = 200, offset: int = 0):
    return manager(request).translation.store.terms(query, target, enabled_only, limit, offset)


@router.post("/terms", status_code=201)
def create_term(body: TermInput, request: Request):
    return manager(request).translation.store.add_term(**body.model_dump())


@router.patch("/terms/{identity}")
def update_term(identity: str, body: TermUpdate, request: Request):
    return manager(request).translation.store.update_term(identity, body.model_dump(exclude_unset=True))


@router.delete("/terms/{identity}")
def delete_term(identity: str, request: Request):
    return manager(request).translation.store.disable_term(identity)


# -------------------------------------------------------------------- memory


@router.get("/memory")
def memory(request: Request, query: str = "", target: str = "", enabled_only: bool = True,
           limit: int = 200, offset: int = 0):
    return manager(request).translation.store.memory(query, target, limit, enabled_only, offset)


@router.post("/memory", status_code=201)
def create_memory(body: MemoryInput, request: Request):
    return manager(request).translation.store.remember(**body.model_dump())


@router.patch("/memory/{identity}")
def update_memory(identity: str, body: MemoryUpdate, request: Request):
    return manager(request).translation.store.update_memory(
        identity, body.model_dump(exclude_unset=True, exclude_none=True))


@router.delete("/memory/{identity}")
def delete_memory(identity: str, request: Request):
    return manager(request).translation.store.disable_memory(identity)


# --------------------------------------------------------------------- agent


@router.post("/agent/translate")
def agent_translate(body: AgentTranslateRequest, request: Request):
    translation = manager(request).translation
    return translation.agent.translate(body.session_id, body.target, body.provider, body.use_memory)


@router.post("/agent/chat")
def agent_chat(body: AgentChatRequest, request: Request):
    return manager(request).translation.agent.chat(body.message, body.session_id, body.target)


@router.post("/sessions/{identity}/apply")
def apply_external(identity: str, body: ApplyTranslationRequest, request: Request):
    return manager(request).translation.apply_external(
        identity, body.target, body.provider, body.model, body.lines)


@router.get("/agent/runs")
def agent_runs(request: Request, session_id: str = "", limit: int = 100):
    return manager(request).translation.store.runs(session_id, limit)


@router.get("/agent/runs/{identity}")
def agent_run(identity: str, request: Request):
    return manager(request).translation.store.get_run(identity)
