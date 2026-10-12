"""Resolve and validate a preset into a generation snapshot and provenance."""
from dataclasses import dataclass
import secrets
from ..config import ConfigStore, voice_fingerprint
from ..runner import check_voice
from ..model_info import model_ui, selected_mode
from ..generation_options import compatible_options
from ..schemas import VoiceConfig


@dataclass(frozen=True)
class PreparedGeneration:
    voice: dict
    fingerprint: str
    provenance: dict


def prepare_generation(config: ConfigStore, model_name: str, overrides=None, use_preset=True, validator=None):
    """Prepare one voice without queuing jobs; caller holds the submission lock.

    Presets remain unchanged. Random seeds and reference copies belong to this
    submission snapshot, which can also be prepared for individual dialogue rows.
    """
    voice, _ = config.resolve(model_name, allow_missing_reference=overrides is not None and "reference_audio" in overrides)
    if overrides and "request_options" in overrides:
        changed_model = any(name in overrides and overrides[name] != voice.get(name) for name in ("family", "model_dir", "engine_executable"))
        previous = {} if changed_model else voice.get("request_options", {})
        overrides = {**overrides, "request_options": {**previous, **overrides["request_options"]}}
    base = voice
    voice = dict(voice, **(overrides or {}))
    info = model_ui(voice)
    voice["request_options"], _ = compatible_options(voice, info)
    (validator or check_voice)(voice)
    # Capture the source preset before seed materialization; snapshots never follow later edits.
    before, after = VoiceConfig(**base).model_dump(), VoiceConfig(**voice).model_dump()
    specs = info.get("generation_options", [])
    defaults = {s["name"]: s.get("default") for s in specs}
    for value in (before, after):
        options, _ = compatible_options(value, info)
        value["request_options"] = {**defaults, **options}
        value["voice_mode"] = selected_mode(value, info)
    changed_model = config.model_identity(base) != config.model_identity(voice)
    changed_mode = selected_mode(base, model_ui(base)) != selected_mode(voice, info)
    preset_used = use_preset and not base.get("is_template", False) and not changed_model and not changed_mode
    provenance = {"preset_used": preset_used, "preset_id": base["id"] if preset_used else None,
                  "preset_name": base["name"] if preset_used else None,
                  "preset_modified": preset_used and before != after,
                  "mode": selected_mode(voice, info)}
    if voice.get("request_options", {}).get("seed") == -1:
        # Persist the actual random seed so retry reuses it and a new take gets a new cache key.
        voice["request_options"] = {**voice["request_options"], "seed": secrets.randbelow(2147483648)}
    if provenance["mode"] in ("clone", "continuation") and voice.get("reference_audio"):
        # A queued job must remain runnable after the caller moves its original audio.
        target, _ = config.copy_reference_audio(voice["reference_audio"])
        voice["reference_audio"] = str(target)
    fingerprint = voice_fingerprint(voice)
    return PreparedGeneration(voice, fingerprint, provenance)
