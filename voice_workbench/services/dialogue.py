"""Stable dialogue identity and submission revisions."""
import hashlib
import json


def dialogue_id(identity):
    return "D-" + identity[:8].upper()


def role_names(editor_state):
    return {entry["id"]: entry["role_name"].strip()
            for entry in (editor_state or {}).get("dialogue_entries", [])
            if entry.get("role_name", "") and entry["role_name"].strip()}


def audio_name(item):
    provenance = item.get("provenance") or {}
    voice = item.get("voice") or {}
    name = provenance.get("preset_name") or voice.get("name") or "当前参数"
    return (item.get("source_name") or dialogue_id(item["source_id"])) + " · " + name if item.get("source_id") else name


def entry_revision(entry, voices, default_voice=None):
    identity = entry.get("voice_id") or entry.get("model_id")
    voice = next((v for v in voices if v["id"] == identity), None) if identity else default_voice
    voice = {**(voice or {}), **(entry.get("overrides") or {})}
    effective = {k: v for k, v in (voice or {}).items() if k not in ("model_ui", "error", "is_template", "name")}
    raw = json.dumps({"text": entry["text"].strip(), "voice_id": entry.get("voice_id"), "voice": effective},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
