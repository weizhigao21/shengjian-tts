"""IndexTTS expression settings and adapter contracts; standard library only."""
import json
import math

EMOTION_FIELDS = ("emotion_mode", "emotion_strength", "emotion_vector",
                  "emotion_reference_audio", "emotion_infer_from_text")
EMOTION_DEFAULTS = {"emotion_mode": "auto", "emotion_strength": .6,
                    "emotion_vector": [0.] * 8, "emotion_reference_audio": "",
                    "emotion_infer_from_text": False}
EMOTION_NAMES = ("高兴", "愤怒", "悲伤", "恐惧", "厌恶", "低落", "惊讶", "平静")
# Eight native dimensions, in the order documented by IndexTTS. Compound
# presets are UI shortcuts, rather than additional model categories.
EMOTION_PRESETS = {
    "平静": [0, 0, 0, 0, 0, 0, 0, .6],
    "愉快": [.6, 0, 0, 0, 0, 0, .1, 0],
    "温柔": [.15, 0, 0, 0, 0, 0, 0, .5],
    "惊喜": [.4, 0, 0, 0, 0, 0, .35, 0],
    "伤感": [0, 0, .35, 0, 0, .2, 0, .15],
    "紧张": [0, 0, 0, .35, 0, 0, .2, 0],
    "愤怒": [0, .5, 0, 0, 0, 0, 0, 0],
    "悲伤": [0, 0, .5, 0, 0, 0, 0, 0],
    "恐惧": [0, 0, 0, .45, 0, 0, 0, 0],
    "厌恶": [0, 0, 0, 0, .45, 0, 0, 0],
    "低落": [0, 0, 0, 0, 0, .5, 0, 0],
    "惊讶": [0, 0, 0, 0, 0, 0, .5, 0],
}


def emotion_mode(voice):
    mode = voice.get("emotion_mode", "auto")
    return ("text" if voice.get("emotion", "").strip() else "speaker") if mode == "auto" else mode


def validate_emotion(voice):
    mode = emotion_mode(voice)
    if mode not in ("speaker", "reference", "vector", "text"):
        raise ValueError("请选择有效的表达方式")
    strength = voice.get("emotion_strength", .6)
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("情绪强度需要在 0–100% 之间")
    if mode == "vector":
        vector = voice.get("emotion_vector", [0.] * 8)
        if not isinstance(vector, (list, tuple)) or len(vector) != 8 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in vector):
            raise ValueError("情绪微调需要 8 个 0–100% 的数值")
    if mode == "reference" and not voice.get("emotion_reference_audio"):
        raise ValueError("参考演绎需要单独选择演绎参考音频")
    if mode == "text" and not voice.get("emotion_infer_from_text", False) and not voice.get("emotion", "").strip():
        raise ValueError("请填写情绪描述，或启用从正文推断情绪")
    return mode


def native_emotion(voice):
    mode = validate_emotion(voice)
    options = {} if mode == "speaker" else {"emotion_alpha": voice.get("emotion_strength", .6)}
    if mode == "vector":
        options["emotion_vector"] = json.dumps(voice.get("emotion_vector", [0.] * 8))
    elif mode == "text":
        options["use_emotion_text"] = True
        if not voice.get("emotion_infer_from_text", False):
            options["emotion_text"] = voice["emotion"].strip()
    return options, voice["emotion_reference_audio"] if mode == "reference" else None


def python_emotion(voice):
    mode = validate_emotion(voice)
    options = {} if mode == "speaker" else {"emo_alpha": voice.get("emotion_strength", .6)}
    if mode == "reference":
        options["emo_audio_prompt"] = voice["emotion_reference_audio"]
    elif mode == "vector":
        options["emo_vector"] = list(voice.get("emotion_vector", [0.] * 8))
    elif mode == "text":
        options["use_emo_text"] = True
        if not voice.get("emotion_infer_from_text", False):
            options["emo_text"] = voice["emotion"].strip()
    return options
