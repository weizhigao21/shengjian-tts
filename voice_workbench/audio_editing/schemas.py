from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class EditState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(default="未命名音频", min_length=1, max_length=200)
    original_text: str = Field(default="", max_length=20000)
    target_text: str = Field(default="", max_length=20000)
    selected_version: str | None = None


class AudioImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    name: str = Field(default="", max_length=200)
    original_text: str = Field(default="", max_length=20000)


class AudioOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source_version: str
    operation: Literal["render", "append", "vc", "edit"] = "render"
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    gain_db: float = Field(default=0, ge=-36, le=12)
    fade_in: float = Field(default=0, ge=0, le=10)
    fade_out: float = Field(default=0, ge=0, le=10)
    append_path: str | None = None
    model_path: str | None = None
    reference_path: str | None = None
    device: Literal["cuda", "cpu"] = "cuda"
    seed: int = Field(default=-1, ge=-1, le=4294967295)
    steps: int = Field(default=32, ge=1, le=100)
    state: EditState


class EditModelImport(BaseModel):
    path: str
