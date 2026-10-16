from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class BatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    python: str = Field(default="", max_length=4096)
    model_dir: str = Field(default="", max_length=4096)
    source_model: str = Field(default="", max_length=4096)
    device: Literal["cuda", "cpu"] = "cuda"
    dtype: Literal["auto", "float16", "bfloat16", "float32"] = "auto"
    max_batch_size: int = Field(default=2, ge=2, le=8)
    max_batch_chars: int = Field(default=2400, ge=100, le=12000)
    prompt_cache_slots: int = Field(default=4, ge=1, le=16)
    idle_seconds: int = Field(default=120, ge=30, le=3600)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: str = "default"
    texts: list[str] = Field(min_length=1, max_length=10000)
