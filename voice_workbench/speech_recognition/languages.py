"""Resolve ASR output languages for the native forced-aligner contract."""
LANGUAGE_CHOICES = [("自动判断", "auto"), ("中文", "zh"), ("英语", "en"), ("粤语", "yue"), ("日语", "ja"),
                    ("韩语", "ko"), ("德语", "de"), ("法语", "fr"), ("西班牙语", "es"), ("俄语", "ru"),
                    ("葡萄牙语", "pt"), ("意大利语", "it")]
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
