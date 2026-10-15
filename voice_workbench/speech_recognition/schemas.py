"""Request and state payloads for the recognition mode."""
from pydantic import BaseModel, ConfigDict, Field


class RecognizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    name: str = Field(default="", max_length=200)
    language: str = Field(default="auto", max_length=20)


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(default="未命名音频", min_length=1, max_length=200)
    language: str = Field(default="auto", max_length=20)


class SelectionRequest(BaseModel):
    """Defaults for the next run; a null field leaves the stored choice unchanged."""

    model_config = ConfigDict(extra="forbid")
    asr_model: str | None = Field(default=None, max_length=1024)
    align_model: str | None = Field(default=None, max_length=1024)
    language: str | None = Field(default=None, max_length=20)
