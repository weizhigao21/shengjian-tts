"""Collapse the one-token loops an ASR decoder falls into on non-speech audio.

Qwen3-ASR can enter a degenerate repeat loop when a window is music, applause or
steady noise instead of speech: the transcript becomes ``oh, oh, oh, ...`` for tens
of seconds. The aligner then faithfully gives every repeated token its own time, so
the line builder turns one model failure into twenty identical subtitle lines.

This module removes that loop after recognition, before lines are built:

* consecutive identical word units (six or more) are folded into one;
* as a fallback for estimated timing — where one unit may hold a whole paragraph —
  each finished line's text is compressed the same way;
* adjacent lines that are still identical (three or more) are folded into one.

Only runs are touched. A word repeated naturally elsewhere in the script keeps every
other occurrence. The batch page exposes this as a switch; it is on by default.
"""
from __future__ import annotations

import re

from .lines import reattach_punctuation

# A real speaker may repeat a word for emphasis; a decoder loop is longer than that.
MIN_REPEAT = 6
# Lines that are byte-identical this many times in a row are a loop, not a chorus.
MIN_LINE_REPEAT = 3

# Latin words with optional apostrophes, and one CJK character — the two token shapes
# whose repetition can be spotted without a language-specific tokenizer. Other scripts
# are covered by the aligned-unit pass whenever an aligner is installed.
_WORD = re.compile(r"[A-Za-z0-9']+|[\u3400-\u9fff]")
_NO_WORD = r"[^\w\u3400-\u9fff]"


def _key(text):
    """Normalised comparison key: case, spacing and punctuation do not matter."""
    return "".join(character for character in str(text or "") if character.isalnum()).lower()


def _line_key(text):
    return _key(text) or str(text or "").strip().lower()


def dedupe_units(units, min_repeat=MIN_REPEAT):
    """Fold runs of the same aligned word; returns ``(units, removed)``.

    ``units`` are ``(text, start, end)`` triples. Only runs longer than
    ``min_repeat`` are touched, so a short deliberate repetition survives. Keeping
    the first unit also keeps its own start time; the rest of the loop becomes
    silence, which is exactly what was there in the audio.
    """
    result, removed = [], 0
    index = 0
    while index < len(units):
        word, start, end = units[index]
        key = _key(word)
        last = index + 1
        while key and last < len(units) and _key(units[last][0]) == key:
            last += 1
        if key and last - index >= min_repeat:
            result.append(units[index])
            removed += last - index - 1
        else:
            result.extend(units[index:last])
        index = last
    return result, removed


def _collapse_text(text, min_repeat=MIN_REPEAT):
    removed = [0]
    repeats = max(1, int(min_repeat) - 1)

    def replace(match):
        words = _WORD.findall(match.group(0))
        removed[0] += max(0, len(words) - 1)
        return match.group(1) + match.group(2)

    # The separator is intentionally not back-referenced: a loop's final token often
    # has no trailing space (``oh, oh, oh``), and requiring the first separator
    # exactly would leave that last token behind.
    latin = re.compile(rf"(?i)\b([A-Za-z0-9']+)({_NO_WORD}*)(?:\1{_NO_WORD}*){{{repeats},}}")
    cjk = re.compile(rf"([\u3400-\u9fff])({_NO_WORD}*)(?:\1{_NO_WORD}*){{{repeats},}}")
    text = latin.sub(replace, text)
    text = cjk.sub(replace, text)
    return text, removed[0]


def dedupe_text(text, min_repeat=MIN_REPEAT):
    """Compress a raw transcript's repeated tokens; returns ``(text, removed)``."""
    if not text:
        return text or "", 0
    return _collapse_text(str(text), min_repeat)


def dedupe_transcript(text, units, min_repeat=MIN_REPEAT):
    """Clean a recognition result before lines are built.

    Returns ``(text, units, removed)``. With aligned units the text is rebuilt from
    the cleaned units, so punctuation and the later line grouping stay consistent.
    Without units the raw transcript is compressed and line timing remains estimated.
    """
    units = list(units or [])
    if units:
        units = reattach_punctuation(units, text)
        units, removed = dedupe_units(units, min_repeat)
        text = "".join(word for word, _, _ in units).strip()
        return text, units, removed
    text, removed = dedupe_text(text, min_repeat)
    return text, [], removed


def dedupe_lines(lines, min_repeat=MIN_REPEAT, min_line_repeat=MIN_LINE_REPEAT):
    """Compress each line's own loop, then fold adjacent identical lines.

    This is the safety net for a result whose alignment failed: such a window is
    estimated as a few large units, so ``dedupe_units`` cannot see the repeated
    token inside them. It also removes the repeated lines a long loop produces even
    when their internal runs were already compressed. Returns ``(lines, removed)``.
    """
    shaped, removed = [], 0
    for line in lines or []:
        value = dict(line)
        compressed, count = dedupe_text(value.get("text") or "", min_repeat)
        if count:
            value["text"] = compressed.strip()
            removed += count
        shaped.append(value)

    result = []
    index = 0
    while index < len(shaped):
        key = _line_key(shaped[index].get("text"))
        last = index + 1
        while key and last < len(shaped) and _line_key(shaped[last].get("text")) == key:
            last += 1
        if key and last - index >= min_line_repeat:
            result.append(shaped[index])
            removed += last - index - 1
        else:
            result.extend(shaped[index:last])
        index = last

    for position, line in enumerate(result, start=1):
        line["index"] = position
    return result, removed
