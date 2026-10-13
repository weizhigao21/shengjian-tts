from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

Language = Literal["zh", "en", "en-us", "en-gb", "ja", "es", "ar", "fr", "fr-fr", "hi", "it", "pt", "pt-br", "ko", "de", "ru"]


EmotionMode = Literal["auto", "speaker", "reference", "vector", "text"]
EmotionWeight = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
EmotionVector = Annotated[list[EmotionWeight], Field(min_length=8, max_length=8)]


class VoiceConfig(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    backend: Literal["windows_sapi", "audio_cpp", "index_tts"] = "windows_sapi"
    python: str
    sapi_voice: str = ""
    repo_dir: str = ""
    model_dir: str = ""
    reference_audio: str = ""
    index_version: Literal["2", "2.5"] = "2"
    device: Literal["cuda", "cpu"] = "cuda"
    revision: str = "1"
    speed: float = Field(default=1, ge=0.5, le=2)
    emotion: str = Field(default="", max_length=500)
    emotion_mode: EmotionMode = "auto"
    emotion_strength: EmotionWeight = .6
    emotion_vector: EmotionVector = Field(default_factory=lambda: [0.] * 8)
    emotion_reference_audio: str = Field(default="", max_length=4096)
    emotion_infer_from_text: bool = False
    language: Language = "zh"
    timeout: int = Field(default=600, ge=10, le=3600)
    engine_executable: str = ""
    family: Literal["index_tts2", "voxcpm2", "kokoro_tts", "qwen3_tts"] = "index_tts2"
    reference_text: str = ""
    native_voice: str = "zf_xiaobei"
    voice_mode: Literal["auto", "builtin", "clone", "design", "continuation"] = "auto"
    voice_description: str = Field(default="", max_length=500)
    load_options: dict = Field(default_factory=dict)
    request_options: dict = Field(default_factory=dict)


class SegmentInput(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    timestamp: str = Field(default="", max_length=50)
    voice_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    source_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    force_generate: bool = False
    use_preset: bool = True
    overrides: GenerationOverrides | None = None


class ScriptMention(BaseModel):
    start: int = Field(ge=0, le=4000000)
    end: int = Field(ge=1, le=4000000)
    preset_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    label: str = Field(min_length=1, max_length=100)


class DialogueEntry(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    role_name: str | None = Field(default=None, max_length=100)
    text: str = Field(default="", max_length=5000)
    voice_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    voice_name: str = Field(default="", max_length=100)
    model_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    overrides: GenerationOverrides | None = None


class EditorState(BaseModel):
    mode: Literal["article", "dialogue"] = "article"
    article_text: str = Field(default="", max_length=2000000)
    article_mentions: list[ScriptMention] = Field(default_factory=list, max_length=10000)
    dialogue_entries: list[DialogueEntry] = Field(default_factory=list, max_length=10000)


class GenerationOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")
    native_voice: str | None = Field(default=None, max_length=100)
    voice_mode: Literal["builtin", "clone", "design", "continuation"] | None = None
    voice_description: str | None = Field(default=None, max_length=500)
    speed: float | None = Field(default=None, ge=.5, le=2)
    emotion: str | None = Field(default=None, max_length=500)
    emotion_mode: EmotionMode | None = None
    emotion_strength: EmotionWeight | None = None
    emotion_vector: EmotionVector | None = None
    emotion_reference_audio: str | None = Field(default=None, max_length=4096)
    emotion_infer_from_text: bool | None = None
    language: Language | None = None
    reference_audio: str | None = Field(default=None, max_length=4096)
    reference_text: str | None = Field(default=None, max_length=5000)
    device: Literal["cuda", "cpu"] | None = None
    timeout: int | None = Field(default=None, ge=10, le=3600)
    family: Literal["index_tts2", "voxcpm2", "kokoro_tts", "qwen3_tts"] | None = None
    model_dir: str | None = Field(default=None, max_length=4096)
    engine_executable: str | None = Field(default=None, max_length=4096)
    python: str | None = Field(default=None, max_length=4096)
    repo_dir: str | None = Field(default=None, max_length=4096)
    request_options: dict | None = None


class JobInput(BaseModel):
    voice_id: str = "default"
    use_preset: bool = True
    title: str = Field(default="配音任务", max_length=200)
    segments: list[SegmentInput] = Field(min_length=1, max_length=10000)
    overrides: GenerationOverrides | None = None
    script: str | None = Field(default=None, max_length=2000000)
    script_mentions: list[ScriptMention] | None = Field(default=None, max_length=10000)
    editor_state: EditorState | None = None
    workflow_key: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.:-]{1,160}$")
    record_only: bool = False
    delivery_ack: bool = False


class PerformanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_workers: int = Field(default=1, ge=1, le=4)
    cpu_threads: int = Field(default=0, ge=0, le=128)


class DocumentInput(BaseModel):
    title: str = Field(default="未命名配音", max_length=200)
    script: str = Field(default="", max_length=2000000)
    script_mentions: list[ScriptMention] | None = Field(default=None, max_length=10000)
    editor_state: EditorState | None = None


class AudioDeletion(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=100000)
    all: bool = False


class ModelDownload(BaseModel):
    source: Literal["huggingface", "mirror", "modelscope"] = "mirror"


class EngineDownload(BaseModel):
    backend: Literal["cuda", "cpu"] = "cuda"


class ModelRemoval(BaseModel):
    delete_file: bool = False


class ModelImport(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class RenameJob(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ReferenceInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    reference_audio: str = Field(min_length=1, max_length=4096)
    reference_text: str = Field(default="", max_length=5000)


class ReferenceEdit(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    reference_text: str = Field(default="", max_length=5000)


class LegacyInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model_name: str = "default"
    text: str | None = Field(default=None, max_length=5000)
    texts: list[str] | None = None
    # 保留原工作流的字段名。未提供时采用声音预设。
    speed_facter: float | None = Field(default=None, ge=0.5, le=2)
    emotion: str | None = Field(default=None, max_length=500)
    text_lang: str | None = None
