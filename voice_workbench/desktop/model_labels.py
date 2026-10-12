"""Stable model identities and display names."""
from pathlib import Path
import json
import os
import re
from ..model_info import model_ui


FAMILIES = {"index_tts2": "IndexTTS", "voxcpm2": "VoxCPM2", "kokoro_tts": "Kokoro", "qwen3_tts": "Qwen3-TTS"}


def model_key(voice):
    return json.dumps([voice["backend"], voice.get("family"),
                       os.path.normcase(os.path.abspath(voice.get("model_dir") or ".")),
                       os.path.normcase(os.path.abspath(voice.get("engine_executable") or voice.get("repo_dir") or ".")),
                       voice.get("index_version") if voice["backend"] == "index_tts" else None])


def model_label(voice):
    if voice["backend"] == "windows_sapi":
        return "Windows 系统声音"
    if voice["backend"] == "index_tts":
        return "IndexTTS " + voice.get("index_version", "2") + " · Python"
    stem = Path(voice.get("model_dir") or "").stem
    name = FAMILIES.get(voice.get("family"), stem)
    if voice.get("family") == "index_tts2":
        name += " 2.5" if "2_5" in stem or "2.5" in stem else " 2"
    elif voice.get("family") == "kokoro_tts" and "82m" in stem.lower():
        name += " 82M"
    elif voice.get("family") == "qwen3_tts":
        info = voice.get("model_ui") or model_ui(voice)
        variant = {"custom_voice": "CustomVoice", "voice_design": "VoiceDesign", "base": "Base"}.get(info.get("variant"), "")
        size = re.search(r"(?:^|[-_])(\d+(?:\.\d+)?b)(?:[-_]|$)", stem, re.I)
        name += (" " + size.group(1).upper() if size else "") + (" " + variant if variant else "")
    precision = re.search(r"(?:q\d+(?:[_-][a-z0-9]+)?|bf16|f16|orig)$", stem, re.I)
    return name + (" · " + precision.group().upper().replace("-", "_") if precision else "")
