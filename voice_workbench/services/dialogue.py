"""Stable dialogue identity and submission revisions."""
import hashlib
import json


def entry_revision(entry, voices, default_voice=None):
    voice = next((v for v in voices if v["id"] == entry.get("voice_id")), None) if entry.get("voice_id") else default_voice
    effective = {k: v for k, v in (voice or {}).items() if k not in ("model_ui", "error", "is_template", "name")}
    raw = json.dumps({"text": entry["text"].strip(), "voice_id": entry.get("voice_id"), "voice": effective},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
