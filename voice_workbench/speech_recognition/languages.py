"""Resolve ASR output languages for the native forced-aligner contract."""
LANGUAGE_NAMES = {"zh": "Chinese", "en": "English", "yue": "Cantonese", "ja": "Japanese",
                  "ko": "Korean", "de": "German", "fr": "French", "es": "Spanish",
                  "ru": "Russian", "pt": "Portuguese", "it": "Italian"}
ALIGN_LANGUAGES = set(LANGUAGE_NAMES.values())


def native_language(value):
    value = str(value or "").strip()
    key = value.casefold()
    if key in ("", "auto", "unknown", "und"):
        return None
    return LANGUAGE_NAMES.get(key) or next((name for name in ALIGN_LANGUAGES if name.casefold() == key), value)


def alignment_language(requested, detected):
    return native_language(requested) or native_language(detected)
