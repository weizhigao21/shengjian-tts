"""Conservative Qwen Base eligibility and compatible, bounded batch grouping."""
import hashlib
import json
from pathlib import Path

from ..config import voice_fingerprint
from ..generation_options import compatible_options
from ..model_info import model_ui, selected_mode

LANGUAGES = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "de": "German",
             "fr": "French", "ru": "Russian", "pt": "Portuguese", "es": "Spanish", "it": "Italian"}
PARAMETERS = {"max_tokens": "max_new_tokens", "do_sample": "do_sample", "temperature": "temperature",
              "top_k": "top_k", "top_p": "top_p", "repetition_penalty": "repetition_penalty",
              "subtalker_do_sample": "subtalker_dosample", "subtalker_temperature": "subtalker_temperature",
              "subtalker_top_k": "subtalker_top_k", "subtalker_top_p": "subtalker_top_p"}
# Real audio.cpp request options owned by layers a batch run does not have: the
# chunked streaming transport (how the server pushes audio to a player) and the
# CustomVoice speaker table. Base voice cloning renders the same samples with or
# without them, and batch runs require Base clone, so they need no adaptation.
NATIVE_ONLY = {"stream_chunk_policy", "stream_frames_per_event", "voice_id"}
SEGMENTATION = {"seed", "text_chunk_size", "text_chunk_mode"}


def options(voice):
    return compatible_options(voice, model_ui(voice))[0]


def compatible(voice):
    """Split saved options into the ones the current model declares and stale ones.

    A preset can outlive an engine downgrade or a model variant switch; keys the
    current contract no longer declares reach neither backend. Benchmarking drops
    them (it never rewrites the preset) so the comparison stays runnable, while
    declared options the batch contract still cannot express are refused below.
    """
    request = options(voice)
    declared = {spec["name"] for spec in model_ui(voice).get("generation_options", [])}
    return ({name: value for name, value in request.items() if name in declared},
            sorted(set(request) - declared))


def unmapped(voice):
    """Request options the batch contract cannot express, native-only ones aside."""
    request = options(voice)
    return sorted(set(request) - set(PARAMETERS) - SEGMENTATION - NATIVE_ONLY)


def native_only(voice):
    return sorted(set(options(voice)) & NATIVE_ONLY)


def unsupported(voice, text, settings):
    if voice.get("backend") != "audio_cpp" or voice.get("family") != "qwen3_tts":
        return "首版批量后端支持 Qwen3-TTS Base 声音克隆。"
    if not settings.source_model or Path(voice["model_dir"]).resolve() != Path(settings.source_model).resolve():
        return "当前 GGUF 尚未关联到批量后端的原始模型。"
    if selected_mode(voice, model_ui(voice)) != "clone":
        return "当前生成方式不支持合批；请使用 Base 声音克隆。"
    if voice.get("device") != settings.device or voice.get("load_options"):
        return "设备或模型加载选项与批量后端不兼容。"
    if voice.get("language") not in LANGUAGES or voice.get("speed", 1) != 1:
        return "当前语言或语速不支持批量后端。"
    request = options(voice)
    if blocked := unmapped(voice):
        return "批量后端不支持这些生成参数：" + "、".join(blocked) + "。"
    if request.get("text_chunk_mode", "default") != "default":
        return "当前长文分段方式需要原生引擎。"
    if len(text) > min(request.get("text_chunk_size") or 8192, settings.max_batch_chars):
        return "文本超过单批长度限制，使用原生引擎分段。"
    return ""


def group_key(voice):
    # Language may vary in a tensor batch. Seeds and all other generation options
    # remain equal; the official backend has one RNG seed per batch, not per row.
    canonical = dict(voice, language="", timeout=0)
    return voice_fingerprint(canonical)


def model_signature(settings, versions=None):
    root = Path(settings.model_dir).resolve()
    paths = sorted({*root.glob("*.json"), *root.glob("*.safetensors"),
                    *root.glob("speech_tokenizer/*.json"), *root.glob("speech_tokenizer/*.safetensors")})
    files = [(str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns) for p in paths]
    from .qwen_fastpath import REVISION
    value = {"protocol": 1, "settings": settings.model_dump(), "files": files, 'runtime_versions': versions or {},
             'predictor_revision': REVISION}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
