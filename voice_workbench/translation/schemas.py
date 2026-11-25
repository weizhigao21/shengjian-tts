"""Request payloads for subtitle translation."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TranslationSettings(BaseModel):
    """Partial update; a null field leaves the stored value unchanged."""

    model_config = ConfigDict(extra="forbid")
    provider: Literal["api", "local"] | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model_name: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=2000)
    target: str | None = Field(default=None, max_length=20)


class TranslationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["api", "local"] | None = None
    target: str | None = Field(default=None, max_length=20)


class TermInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    source_lang: str = Field(default="", max_length=20)
    target_lang: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=500)
    priority: int = Field(default=0, ge=0, le=100)
    preserve_metadata: bool = False


class TermUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str | None = Field(default=None, min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=500)
    priority: int | None = Field(default=None, ge=0, le=100)
    enabled: bool | None = None


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)
    source_lang: str = Field(default="", max_length=20)
    target_lang: str = Field(default="", max_length=20)
    session_id: str = Field(default="", max_length=64)
    provider: str = Field(default="", max_length=20)
    model: str = Field(default="", max_length=200)
    quality: int = Field(default=1, ge=1, le=2)
    preserve_metadata: bool = False


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str | None = Field(default=None, min_length=1, max_length=500)
    quality: int | None = Field(default=None, ge=1, le=2)
    enabled: bool | None = None


class AgentTranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)
    target: str | None = Field(default=None, max_length=20)
    provider: Literal["api", "local"] | None = None
    use_memory: bool = True


class AgentChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=64)
    target: str | None = Field(default=None, max_length=20)


class ExternalTranslationLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    translated: str = Field(default="", max_length=5000)


class ApplyTranslationRequest(BaseModel):
    """A whole-session translation produced by an external MCP agent."""

    model_config = ConfigDict(extra="forbid")
    target: str = Field(default="", max_length=20)
    provider: str = Field(default="external", max_length=40)
    model: str = Field(default="", max_length=200)
    lines: list[ExternalTranslationLine] = Field(min_length=1)
