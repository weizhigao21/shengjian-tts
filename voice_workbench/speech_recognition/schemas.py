"""Request and state payloads for the recognition mode."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecognizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    name: str = Field(default="", max_length=200)
    language: str = Field(default="auto", max_length=20)
    dedupe: bool = True


class BatchRequest(BaseModel):
    """One queue of files; recognition runs in order and exports beside each source."""

    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=200)
    language: str = Field(default="auto", max_length=20)
    format: Literal["srt", "txt", "srt+txt"] = "srt"
    name: str = Field(default="", max_length=200)
    dedupe: bool = True


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


class LineCorrection(BaseModel):
    """One subtitle line rewrite; timecodes are deliberately not editable."""

    model_config = ConfigDict(extra="forbid")
    index: int
    text: str = Field(min_length=1, max_length=2000)
    reason: str = Field(default="", max_length=200)


class LineCorrections(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrections: list[LineCorrection] = Field(min_length=1, max_length=100)
