"""Editable request options; older packages use the verified audio.cpp v0.7.4 adapter contract."""
from copy import deepcopy
import math

from .errors import VoiceError


def number(name, label, default, minimum, maximum, step=1, **extra):
    return dict(name=name, label=label, type="float" if isinstance(step, float) else "int",
                default=default, min=minimum, max=maximum, step=step, **extra)


CHUNK_MODES = {"default": "自动分段", "tag_aware": "保留标签", "japanese": "日语分段", "endline": "按换行分段"}
STRATEGIES = {"continuation": "连续生成", "stateless": "各段独立生成"}
SEED = number("seed", "随机种子", -1, -1, 2147483647, help="固定种子有助于复现生成；-1 为每次随机。")


def chunk_options(default, mode="default", minimum=0):
    return [number("text_chunk_size", "长文分段字数", default, minimum, 12000,
                   help="每条文本在引擎内的分段字数；0 使用模型默认值。字幕行及时间戳保留。"),
            dict(name="text_chunk_mode", label="分段方式", type="enum", default=mode, values=CHUNK_MODES)]


FALLBACK = {
    "voxcpm2": [
        number("guidance_scale", "引导强度", 2.0, .5, 5, .1, slider=True),
        number("num_inference_steps", "推理步数", 10, 1, 50),
        *chunk_options(2048, "tag_aware"),
        number("max_tokens", "最大生成长度", 4096, 1, 4096, help="生成音频 token 上限，不是文字字数。"),
        dict(name="voxcpm2.chunk_strategy", label="长文生成策略", type="enum", default="continuation", values=STRATEGIES,
             help="连续生成沿用前一段状态；独立生成使用相同参考音频，适合普通文本。"),
        number("min_tokens", "最小生成长度", 2, 0, 4096),
        dict(name="retry_badcase", label="异常音频自动重试", type="bool", default=True),
        number("retry_badcase_max_times", "最多重试次数", 3, 1, 10),
        number("retry_badcase_ratio_threshold", "异常长度比例阈值", 6.0, .1, 20, .1),
        SEED,
    ],
    "index_tts2": [
        number("temperature", "采样温度", .8, .05, 2, .05),
        number("top_p", "Top P", .8, .01, 1, .01),
        number("top_k", "Top K", 30, 1, 200),
        number("repetition_penalty", "重复惩罚", 10.0, .1, 20, .1),
        number("max_tokens", "最大生成长度", 1500, 1, 4000),
        number("interval_silence_ms", "段间静音（毫秒）", 200, 0, 2000, 50),
        number("num_beams", "搜索束数", 3, 1, 10),
        number("length_penalty", "长度惩罚", 0.0, -2, 2, .1),
        dict(name="use_random_emotion", label="随机情绪采样", type="bool", default=False,
             help="随机选择情绪样本，保持当前情绪权重；固定随机种子有助于复现。"),
        *chunk_options(0), SEED,
    ],
    "qwen3_tts": [
        *chunk_options(8192), number("max_tokens", "最大生成长度", 8192, 1, 16384),
        dict(name="do_sample", label="启用采样", type="bool", default=True),
        number("temperature", "采样温度", .9, .05, 2, .05),
        number("top_k", "Top K", 50, 1, 200), number("top_p", "Top P", 1.0, .01, 1, .01),
        number("repetition_penalty", "重复惩罚", 1.05, .1, 20, .05),
        dict(name="subtalker_do_sample", label="音频解码器采样", type="bool", default=True),
        number("subtalker_temperature", "解码器温度", .9, .05, 2, .05),
        number("subtalker_top_k", "解码器 Top K", 50, 1, 200),
        number("subtalker_top_p", "解码器 Top P", 1.0, .01, 1, .01), SEED,
    ],
    "kokoro_tts": [number("text_chunk_size", "长文分段字数", 240, 32, 12000), SEED],
}
RESERVED = {"language", "speed", "speaker", "instruct", "instruction", "reference_text", "x_vector_only_mode",
            "duration_factor", "emotion_text", "use_emotion_text", "emotion_alpha", "emotion_vector", "phonemes", "return_timestamps"}


def option_specs(family, declared=None, config=None):
    fallback = {v["name"]: v for v in FALLBACK.get(family, [])}
    if declared is None:
        result = deepcopy(list(fallback.values()))
    else:
        result = []
        for entry in declared:
            name = entry.get("name")
            if not name or name in RESERVED:
                continue
            base = fallback.get(name, fallback.get("voxcpm2.chunk_strategy", {}) if name == "chunk_strategy" else {})
            spec = {**base, **entry}
            spec["label"] = base.get("label", entry.get("label", name))
            if spec.get("type") in ("string", "enum") and (spec.get("values") or spec.get("enum")):
                spec["type"] = "enum"
                spec["values"] = entry.get("values") or entry.get("enum") or base.get("values")
                translations = base.get("values", {})
                if isinstance(translations, dict) and translations:
                    choices = spec["values"]
                    spec["values"] = {value: translations.get(value, choices.get(value, value) if isinstance(choices, dict) else value)
                                      for value in choices}
            if spec.get("type") not in ("int", "float", "bool", "enum"):
                continue
            if name == "seed":
                spec.update(min=-1, default=-1)
            spec.setdefault("default", False if spec["type"] == "bool" else spec.get("min", 0))
            spec.setdefault("max", 2147483647 if spec["type"] == "int" else 10000)
            spec.setdefault("min", 0)
            spec.setdefault("step", 1 if spec["type"] == "int" else .1)
            result.append(spec)
        # Display in the same stable order as the model's controls.
        order = list(fallback)
        result.sort(key=lambda v: order.index(v["name"]) if v["name"] in order else len(order))
    maximum = (config or {}).get("max_length")
    if isinstance(maximum, int) and maximum > 0:
        for spec in result:
            if spec["name"] in ("min_tokens", "max_tokens"):
                spec["max"] = min(spec["max"], maximum)
                spec["default"] = min(spec["default"], spec["max"])
    return result


def compatible_options(voice, info):
    """Translate known old contracts on a copy; keep unknown/invalid inputs strict."""
    options = dict(voice.get("request_options", {}))
    notes = []
    if voice.get("backend") != "audio_cpp":
        return options, notes
    names = {s["name"] for s in info.get("generation_options", [])}
    if (info.get("family") == "qwen3_tts" and info.get("generation_options_source") != "audio.cpp-v0.7.4-adapter"
            and "text_chunk_mode" not in names and options.get("text_chunk_mode") == "default"):
        options.pop("text_chunk_mode")
        notes.append("旧预设的自动分段已交由当前引擎处理，其他参数保留。")
    if info.get("family") == "voxcpm2":
        for old, current in (("voxcpm2.chunk_strategy", "chunk_strategy"), ("chunk_strategy", "voxcpm2.chunk_strategy")):
            if old in options and old not in names and current in names:
                options.setdefault(current, options.pop(old))
                notes.append("长文生成策略已适配当前引擎的参数名称。")
    return options, notes


def validate_options(voice, info):
    specs = {s["name"]: s for s in info.get("generation_options", [])}
    options, _ = compatible_options(voice, info)
    for name, value in options.items():
        if name not in specs:
            raise VoiceError("UNSUPPORTED_GENERATION_OPTION", f"当前模型不支持生成参数：{name}", "切换模型后使用该模型的高级设置。", "参数检查")
        spec, valid = specs[name], True
        type_ = spec["type"]
        if type_ == "bool":
            valid = isinstance(value, bool)
        elif type_ == "enum":
            valid = isinstance(value, str) and value in spec["values"]
        else:
            valid = type(value) is int if type_ == "int" else type(value) in (int, float)
            valid = valid and math.isfinite(value) and spec["min"] <= value <= spec["max"]
        if not valid:
            raise VoiceError("INVALID_GENERATION_OPTION", f"{spec['label']}的值无效：{value}", "按高级设置中的取值范围调整。", "参数检查")
    if options.get("min_tokens", 0) > options.get("max_tokens", specs.get("max_tokens", {}).get("default", 4096)):
        raise VoiceError("INVALID_GENERATION_OPTION", "最小生成长度不能超过最大生成长度", "调整最小或最大生成长度。", "参数检查")
