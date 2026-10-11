"""Read declarative model capabilities and packaged voices without loading weights."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import struct

from .errors import VoiceError
from .generation_options import option_specs, validate_options

LANGUAGES = {"zh": "中文", "en": "English", "en-us": "英语（美式）", "en-gb": "英语（英式）",
             "ja": "日本語", "es": "Español", "fr": "Français", "fr-fr": "Français",
             "hi": "हिन्दी", "it": "Italiano", "pt": "Português", "pt-br": "Português (Brasil)",
             "ar": "العربية", "ko": "한국어", "de": "Deutsch", "ru": "Русский"}
MODES = {"builtin": "内置音色", "clone": "声音克隆", "design": "文本设计音色", "continuation": "精细克隆（带原文）"}
KOKORO_LANGUAGES = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
                    "i": "it", "j": "ja", "p": "pt-br", "z": "zh"}
SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
WANTED = {"audiocpp.model_spec.json", "audiocpp.model_spec.family", "audiocpp.embedded_files.names",
          "audiocpp.embedded_files.offsets", "audiocpp.embedded_files.data"}


class MetadataReader:
    def __init__(self, stream):
        self.stream = stream
        self.size = stream.seek(0, 2)
        stream.seek(0)

    def read(self, size):
        if size < 0 or size > 8 * 1024 * 1024:
            raise ValueError("metadata field exceeds limit")
        data = self.stream.read(size)
        if len(data) != size:
            raise ValueError("truncated GGUF metadata")
        return data

    def skip(self, size):
        target = self.stream.tell() + size
        if size < 0 or target > self.size:
            raise ValueError("GGUF metadata points outside file")
        self.stream.seek(target)

    def unpack(self, format_):
        return struct.unpack("<" + format_, self.read(struct.calcsize("<" + format_)))

    def value(self, type_, keep=True, depth=0):
        if depth > 3:
            raise ValueError("nested metadata exceeds limit")
        if type_ == 8:
            size, = self.unpack("Q")
            if keep:
                return self.read(size).decode("utf-8")
            self.skip(size)
            return None
        if type_ == 9:
            element, count = self.unpack("IQ")
            if element in SCALARS and not keep:
                offset = self.stream.tell()
                self.skip(struct.calcsize(SCALARS[element]) * count)
                return (offset, element, count)
            if count > 200000:
                raise ValueError("metadata array exceeds limit")
            values = [self.value(element, keep, depth + 1) for _ in range(count)]
            return values if keep else None
        if type_ not in SCALARS:
            raise ValueError("unknown GGUF metadata type")
        return self.unpack(SCALARS[type_])[0]


@lru_cache(maxsize=64)
def _read_package(path, size, modified):
    with Path(path).open("rb") as stream:
        reader = MetadataReader(stream)
        if reader.read(4) != b"GGUF":
            raise ValueError("not a GGUF model")
        version, _, count = reader.unpack("IQQ")
        if version not in (2, 3) or count > 100000:
            raise ValueError("unsupported GGUF header")
        metadata = {}
        for _ in range(count):
            key = reader.value(8)
            type_, = reader.unpack("I")
            keep = key in WANTED and key != "audiocpp.embedded_files.data"
            value = reader.value(type_, keep)
            if key in WANTED:
                metadata[key] = value
        spec = json.loads(metadata.get("audiocpp.model_spec.json", "{}"))
        if not isinstance(spec, dict):
            raise ValueError("model specification must be a JSON object")
        result = {"spec": spec, "family": metadata.get("audiocpp.model_spec.family", spec.get("family")), "files": {}}
        names = metadata.get("audiocpp.embedded_files.names", [])
        offsets = metadata.get("audiocpp.embedded_files.offsets", [])
        blob = metadata.get("audiocpp.embedded_files.data")
        if isinstance(blob, tuple) and blob[1] == 0 and len(offsets) == len(names) + 1:
            for name in ("voices.json", "config.json"):
                if name not in names:
                    continue
                index = names.index(name)
                begin, end = offsets[index:index+2]
                if 0 <= begin <= end <= blob[2] and end - begin <= 2 * 1024 * 1024:
                    stream.seek(blob[0] + begin)
                    content = json.loads(reader.read(end - begin))
                    if not isinstance(content, dict):
                        raise ValueError(f"{name} must be a JSON object")
                    result["files"][name] = content
        return result


def model_ui(voice):
    backend, family = voice.get("backend"), voice.get("family", "index_tts2")
    if backend != "audio_cpp":
        official = backend == "index_tts"
        return {"family": family if official else "windows_sapi", "source": "adapter", "modes": ["clone"] if official else ["builtin"],
                "languages": ["zh", "en", "ja", "es", "ar"] if official and voice.get("index_version") == "2.5" else ["zh", "en"],
                "builtin_voices": [], "speed": not official or voice.get("index_version") == "2.5", "emotion": official,
                "reference_text": False, "language_auto": False, "generation_options": []}
    package, warning = {}, ""
    path = Path(voice.get("model_dir") or "")
    try:
        stat = path.stat()
        package = _read_package(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except (OSError, ValueError, TypeError, KeyError, struct.error) as exc:
        warning = f"模型配置暂时无法读取：{exc}"
    spec = package.get("spec", {})
    source = "gguf" if spec else "adapter"
    if not spec.get("schema_version"):
        try:
            current = json.loads((Path(voice.get("engine_executable") or "").parent / "model_specs" / f"{family}.json").read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                raise ValueError("runtime specification must be a JSON object")
            spec = {**spec, **current}
            source = "runtime+gguf" if package else "runtime"
        except (OSError, ValueError):
            spec = {}
    actual_family = package.get("family") or spec.get("family")
    mismatch = actual_family is not None and actual_family != family
    declared = spec.get("capabilities", {})
    if not isinstance(declared, dict):
        declared = {}
    features = {feature for group in declared.values() if isinstance(group, list) for feature in group}
    # Model declarations are intersected with the modes this adapter can execute.
    if family == "kokoro_tts":
        modes = ["builtin"]
    elif family == "index_tts2":
        modes = ["clone"]
    elif family == "voxcpm2":
        modes = ["clone", "continuation"]
        if "voice_design" in features:
            modes.append("design")
    else:
        # Qwen packages share a family but Base / CustomVoice / VoiceDesign differ.
        config = package.get("files", {}).get("config.json", {})
        variant = config.get("tts_model_type", "base")
        modes = ["design"] if variant == "voice_design" else ["builtin"] if variant == "custom_voice" else ["clone"]
    ui = spec.get("ui", {})
    builtin = ui.get("builtin_voices", []) if isinstance(ui, dict) else []
    if not isinstance(builtin, list):
        builtin = []
    actual_voices = package.get("files", {}).get("voices.json")
    if isinstance(actual_voices, dict):
        builtin = list(actual_voices)
    builtin = [v for v in builtin if isinstance(v, str)]
    if family == "qwen3_tts" and modes == ["builtin"]:
        builtin = list(package.get("files", {}).get("config.json", {}).get("spk_id", {}))
    if family == "kokoro_tts":
        languages = list(dict.fromkeys(KOKORO_LANGUAGES[v[0]] for v in builtin if v and v[0] in KOKORO_LANGUAGES))
    else:
        languages = [v for v in spec.get("languages", ["zh", "en", "ja", "es", "ar"]) if v in LANGUAGES]
    declared_options = spec.get("options", {})
    options = declared_options.get("request", []) if isinstance(declared_options, dict) else []
    options = [v for v in options if isinstance(v, dict)] if isinstance(options, list) else []
    names = {v.get("name") for v in options}
    declared = options if isinstance(declared_options, dict) and "request" in declared_options else None
    if mismatch:
        warning = f"模型文件属于 {actual_family}，当前选择了 {family}，请修改模型家族。"
    return {"family": family, "actual_family": actual_family, "source": source, "warning": warning,
            "mismatch": mismatch, "display_name": spec.get("display_name", family), "modes": modes,
            "builtin_voices": builtin, "languages": languages, "language_auto": family == "voxcpm2",
            "speed": family == "index_tts2" or (family == "kokoro_tts" and "speed" in names),
            "emotion": family == "index_tts2" and ("emotion_control" in features or not spec),
            "reference_text": family in ("voxcpm2", "qwen3_tts"), "request_options": options,
            "generation_options": option_specs(family, declared, package.get("files", {}).get("config.json")),
            "generation_options_source": source if declared is not None else "audio.cpp-v0.7.4-adapter"}


def selected_mode(voice, info=None):
    info = info or model_ui(voice)
    mode = voice.get("voice_mode", "auto")
    return info["modes"][0] if mode == "auto" else mode


def validate_capabilities(voice, info=None):
    info = info or model_ui(voice)
    validate_options(voice, info)
    mode = selected_mode(voice, info)
    if info.get("mismatch"):
        raise VoiceError("MODEL_FAMILY_MISMATCH", info["warning"], "让模型家族与 GGUF 内嵌配置一致。", "配置检查")
    if mode not in info["modes"]:
        raise VoiceError("UNSUPPORTED_VOICE_MODE", f"该模型不支持{MODES.get(mode, mode)}", "选择模型支持的生成方式。", "配置检查")
    if voice.get("voice_description") and not (mode == "design" or (info["family"] == "voxcpm2" and mode == "clone") or (info["family"] == "qwen3_tts" and mode == "builtin")):
        raise VoiceError("UNSUPPORTED_VOICE_DESIGN", "该模型或生成方式不支持声音描述", "使用模型支持的内置音色或参考声音。", "配置检查")
    if mode == "design" and not voice.get("voice_description", "").strip():
        raise VoiceError("VOICE_DESCRIPTION_REQUIRED", "文本设计音色需要声音描述", "填写声音的性别、音色和风格。", "配置检查")
    if mode == "continuation" and not voice.get("reference_text", "").strip():
        raise VoiceError("REFERENCE_TEXT_REQUIRED", "精细克隆需要参考音频原文", "填写与参考音频一致的文字。", "配置检查")
    if not info["speed"] and voice.get("speed", 1) != 1:
        raise VoiceError("UNSUPPORTED_SPEED", "该模型配置未提供语速控制", "将语速恢复为 1。", "配置检查")
    if not info["emotion"] and voice.get("emotion"):
        raise VoiceError("UNSUPPORTED_EMOTION", "该模型不支持情绪描述", "使用支持情绪控制的模型，或清空情绪描述。", "配置检查")
    if voice.get("backend") == "audio_cpp" and mode == "builtin":
        native = voice.get("native_voice")
        if not native or native not in info["builtin_voices"]:
            raise VoiceError("BUILTIN_VOICE_NOT_FOUND", f"模型包中没有内置音色：{native}", "从该模型的内置音色列表选择。", "配置检查")
    elif not info["language_auto"] and info["languages"] and voice.get("language") not in info["languages"]:
        raise VoiceError("UNSUPPORTED_LANGUAGE", "该模型不支持所选语言", "选择模型支持的语言。", "配置检查")
    return mode
