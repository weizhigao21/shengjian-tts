"""Saved voice discovery for the bilingual workflow, without loading model weights."""
from pathlib import Path

from . import __version__
from .config import voice_fingerprint
from .errors import VoiceError
from .model_info import LANGUAGES, MODES, model_ui, selected_mode
from .runner import check_voice


def voice_records(config):
    result = []
    for voice in config.voices():
        try:
            digest = voice_fingerprint(voice)
            check_voice(voice)
            result.append({**voice, "model_name": f"{voice['id']}@{digest[:12]}",
                           "model_tag": digest, "error": None, "model_ui": model_ui(voice)})
        except VoiceError as exc:
            result.append({**voice, "model_name": voice["id"], "model_tag": "",
                           "error": exc.as_dict(), "model_ui": model_ui(voice)})
    return result


def workflow_manifest(config, base_url):
    voices = []
    for voice in voice_records(config):
        if voice.get("is_template"):
            continue
        info = voice["model_ui"]
        mode = selected_mode(voice, info)
        if voice["backend"] == "windows_sapi":
            model = "Windows 系统声音"
        elif voice["backend"] == "index_tts":
            model = "IndexTTS " + voice.get("index_version", "2") + " · Python"
        else:
            model = Path(voice.get("model_dir") or "").stem
        language = "" if info.get("language_auto") else voice.get("language", "")
        voices.append({"id": voice["id"], "name": voice["name"], "model": model,
                       "model_name": voice["model_name"], "model_tag": voice["model_tag"],
                       "mode": mode, "mode_name": MODES.get(mode, mode),
                       "language": language, "language_name": LANGUAGES.get(language, language or "自动识别"),
                       "native_voice": voice.get("native_voice", "") if mode == "builtin" and voice["backend"] == "audio_cpp" else "",
                       "available": voice["error"] is None, "error": voice["error"]})
    base = base_url.rstrip("/")
    return {"schema_version": 1, "service": "local-voice-workbench", "name": "声笺",
            "version": __version__, "base_url": base, "discovery_url": base + "/workflow",
            "capabilities": ["voice_discovery", "async_jobs", "workflow_record_only"], "voices": voices,
            "available_count": sum(v["available"] for v in voices)}
