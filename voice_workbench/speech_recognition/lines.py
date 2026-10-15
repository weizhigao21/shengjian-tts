"""Turn a transcript plus word timings into the display lines the UI plays back.

The ASR family returns plain text; the aligner returns word (or segment) units with
time ranges. Lines are rebuilt from those units so that clicking a line plays
exactly the span it covers.
"""
from __future__ import annotations

SENTENCE_END = "。！？!?…"
CLAUSE_END = "，,、；;：:"
TRAILING = "”』」）)】》\"'"
# Space-separated scripts end sentences with an ASCII period, which the aligner also
# drops (English "position." came back as "position"). It is handled separately so a
# decimal point — tokenised as "3", ".", "5" — does not split a number across lines.
PERIOD = "."

# Qwen3 Forced Aligner emits frame indices on a 16 kHz timeline. Verified against the
# native engine: a 48 kHz clip came back ending at 1.600 s instead of 4.770 s — exactly
# 16000/48000 of the truth — while ``start_sample`` stayed correct at 16 kHz for every
# input rate tried (16k/22.05k/24k/32k/44.1k/48k). Sample indices are therefore the
# trustworthy field and the seconds fields are only a fallback.
ALIGNER_SAMPLE_RATE = 16000
SAMPLE_KEYS = (("start_sample", "end_sample"), ("start_samples", "end_samples"))

# Fallbacks when the transcript arrives without usable word units.
MAX_LINE_CHARS = 24
MAX_LINE_SECONDS = 8.0
MAX_GAP_SECONDS = 1.2
# A clause mark only ends a line once that line is already long enough to read, so
# short comma-separated phrases stay together and sentences remain the main unit.
MIN_CLAUSE_BREAK_CHARS = 14
# Space-separated scripts need a wider line: 24 characters is barely four English
# words, while it is a comfortable Chinese half-sentence. Mixed text keeps the
# narrower width.
ASCII_MAX_LINE_CHARS = 42
CJK_MIN_SHARE = 0.2


def _as_seconds(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _text_of(unit):
    for key in ("word", "text", "token", "content"):
        value = unit.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _range_of(unit):
    for start_key, end_key in SAMPLE_KEYS:
        first, last = unit.get(start_key), unit.get(end_key)
        if isinstance(first, int) and isinstance(last, int) and not isinstance(first, bool):
            start = first / ALIGNER_SAMPLE_RATE
            return start, max(start, last / ALIGNER_SAMPLE_RATE)
    start = next((_as_seconds(unit[k]) for k in ("start", "start_time", "begin", "from") if k in unit), None)
    end = next((_as_seconds(unit[k]) for k in ("end", "end_time", "finish", "to") if k in unit), None)
    if start is None and end is None:
        return None
    start = 0.0 if start is None else start
    end = start if end is None else end
    # Some payloads report milliseconds; a clip longer than a day is not plausible.
    if max(start, end) > 86400:
        start, end = start / 1000.0, end / 1000.0
    return start, max(start, end)


def extract_units(payload):
    """Collect ``(text, start, end)`` units from any of the engine's timing shapes."""
    if not isinstance(payload, dict):
        return []
    for key in ("words", "word_timestamps", "tokens", "segments", "alignment", "items", "chunks"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            units = []
            for item in candidate:
                if not isinstance(item, dict):
                    continue
                text = _text_of(item)
                span = _range_of(item)
                if text and span:
                    units.append((text, span[0], span[1]))
            if units:
                return units
    return []


def reattach_punctuation(units, text):
    """Give each unit the marks the aligner drops so line breaks can follow sentences.

    The aligner returns characters only — 89 units for a 100-character transcript — so
    ``。``/``，`` never reach the grouping step and lines would break purely on length.
    Skipped characters are handed to the unit they follow, which is where Chinese
    punctuation belongs.
    """
    if not text or not units:
        return list(units)
    merged, cursor = [], 0
    for word, start, end in units:
        position = text.find(word, cursor)
        if position < 0:
            position = text.lower().find(word.lower(), cursor)
        if position < 0:
            merged.append((word, start, end))
            continue
        matched = text[position:position + len(word)]
        skipped = text[cursor:position]
        if skipped and merged:
            previous, first, last = merged[-1]
            merged[-1] = (previous + skipped, first, last)
        merged.append((skipped + word if skipped and not merged else word, start, end))
        cursor = position + len(matched)
    tail = text[cursor:]
    if tail and merged:
        previous, first, last = merged[-1]
        merged[-1] = (previous + tail, first, last)
    return merged


def _is_cjk(character):
    return "\u3400" <= character <= "\u9fff" or "\uf900" <= character <= "\ufaff"


def _line_width(text, max_chars):
    """Pick a line width that suits the script the transcript is written in."""
    if max_chars != MAX_LINE_CHARS:
        return max_chars
    letters = sum(1 for character in text if character.isalnum())
    cjk = sum(1 for character in text if _is_cjk(character))
    if not letters or cjk / letters >= CJK_MIN_SHARE:
        return MAX_LINE_CHARS
    return ASCII_MAX_LINE_CHARS


def _is_decimal(core, following):
    """``"3."`` followed by ``"5"`` is one number, not the end of a sentence."""
    head = core[:-1]
    return bool(head) and head[-1].isdigit() and bool(following) and following[:1].isdigit()


def _ends_line(part, gap_after, length=0, following=None):
    stripped = part.rstrip()
    if not stripped:
        return False
    core = stripped.rstrip(TRAILING)
    if core.endswith(tuple(SENTENCE_END)):
        return True
    if len(core) > 1 and core.endswith(PERIOD) and not _is_decimal(core, following):
        return True
    if core.endswith(tuple(CLAUSE_END)) and length >= MIN_CLAUSE_BREAK_CHARS:
        return True
    return gap_after is not None and gap_after >= MAX_GAP_SECONDS


def build_lines(units, total_duration=None, text="", max_chars=MAX_LINE_CHARS, max_seconds=MAX_LINE_SECONDS):
    """Group time-stamped units into readable, playable lines."""
    units = reattach_punctuation(units, text)
    width = _line_width(text, max_chars)
    lines, current = [], []
    for position, (part, start, end) in enumerate(units):
        current.append((part, start, end))
        following = units[position + 1] if position + 1 < len(units) else None
        gap = (following[1] - end) if following else None
        length = len("".join(item for item, _, _ in current))
        span = end - current[0][1]
        if (_ends_line(part, gap, length, following[0] if following else None)
                or length >= width or span >= max_seconds or following is None):
            lines.append(current)
            current = []
    if current:
        lines.append(current)
    return _shape(lines, total_duration)


def _shape(groups, total_duration):
    result = []
    for index, group in enumerate(groups, start=1):
        text = "".join(part for part, _, _ in group).strip()
        if not text:
            continue
        start = max(0.0, group[0][1])
        end = max(start, group[-1][2])
        if total_duration:
            end = min(end, total_duration)
        result.append({"index": len(result) + 1, "start": round(start, 3), "end": round(end, 3), "text": text})
    return result


def fallback_lines(text, duration):
    """Split a plain transcript on punctuation, spreading time by character share."""
    import re
    chunks = [chunk for chunk in re.split(r"(?<=[。！？!?…；;\n])|(?<=\.)(?=\s)", text or "") if chunk.strip()]
    if not chunks:
        return []
    total = sum(len(chunk.strip()) for chunk in chunks) or 1
    lines, cursor = [], 0.0
    for chunk in chunks:
        share = len(chunk.strip()) / total
        span = (duration or 0.0) * share
        lines.append({"index": len(lines) + 1, "start": round(cursor, 3),
                      "end": round(cursor + span, 3), "text": chunk.strip()})
        cursor += span
    return lines
