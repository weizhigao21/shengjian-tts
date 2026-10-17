from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class BatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    python: str = Field(default="", max_length=4096)
    model_dir: str = Field(default="", max_length=4096)
    source_model: str = Field(default="", max_length=4096)
    device: Literal["cuda", "cpu"] = "cuda"
    dtype: Literal["auto", "float16", "bfloat16", "float32"] = "auto"
    max_batch_size: int = Field(default=2, ge=2, le=8)
    min_batch_size: int = Field(default=2, ge=2, le=8)
    max_batch_chars: int = Field(default=2400, ge=100, le=12000)
    prompt_cache_slots: int = Field(default=4, ge=1, le=16)
    idle_seconds: int = Field(default=120, ge=30, le=3600)

    @model_validator(mode='after')
    def batch_bounds(self):
        if self.min_batch_size > self.max_batch_size:
            raise ValueError('启动合批条数不能大于每批最多条数')
        return self


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: str = "default"
    texts: list[str] = Field(min_length=1, max_length=10000)


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['all', 'runtime', 'model', 'detect'] = 'all'
    source_model: str = Field(default='', max_length=4096)
    device: Literal['cuda', 'cpu'] = 'cuda'
    source: Literal['mirror', 'huggingface'] = 'mirror'


class BenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    voice_id: str = Field(min_length=1, max_length=500)
    workload_name: Literal['bilingual', 'zh', 'en'] = 'bilingual'
    warm_rounds: int = Field(default=1, ge=1, le=3)
