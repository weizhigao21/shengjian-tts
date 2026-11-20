"""Turn a transcript plus word timings into the display lines the UI plays back.

The ASR family returns plain text; the aligner returns word (or segment) units with
time ranges. Lines are rebuilt from those units so that clicking a line plays
exactly the span it covers.
"""
from __future__ import annotations

import math

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
# A clause mark only ends a line once that line is already long enough to read, so
# short comma-separated phrases stay together and sentences remain the main unit.
# This is the granularity a *piece* of a long clip is dealt out at, not a line width.
MIN_CLAUSE_BREAK_CHARS = 14
# The width is a target, not a wall. Reaching it only arms a break; the line keeps
# eating characters until a mark or a pause shows up, so "棉花糖" is not torn into
# "棉花" + "糖". This is how far past the target it may go before cutting blind.
LINE_SLACK_CHARS = 8
# A line shorter than this is a fragment. It is what a pause between two characters
# of one word looks like ("睡" / "着"), so fragments are folded into a neighbour
# instead of being played on their own.
MIN_LINE_CHARS = 6
# A mark may end a line once the line reads on its own. This is lower than the atom
# threshold: "感到愚蠢，" is a fine line even at five characters, and refusing it is
# what pushed a break into the middle of "狂妄".
MIN_LINE_MARK_CHARS = 5
# How far a line may run past MAX_LINE_SECONDS before it is cut regardless.
LINE_SLACK_SECONDS = 1.5
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


def _normalised(text):
    """Strip marks and spaces, keeping a map back to the original offsets.

    Matching a unit against the raw transcript fails as soon as the aligner joins two
    words the transcript separates (it returned ``"sleepBambi"`` where the transcript
    read ``"sleep。Bambi"``). A failed match used to leave the cursor behind, which
    re-emitted text already placed — the duplicated words users saw.
    """
    offsets, letters = [], []
    for position, character in enumerate(text):
        if character.isalnum():
            offsets.append(position)
            letters.append(character.lower())
    return "".join(letters), offsets


def reattach_punctuation(units, text):
    """Give each unit the marks the aligner drops so line breaks can follow sentences.

    The aligner returns characters only — 89 units for a 100-character transcript — so
    ``。``/``，`` never reach the grouping step and lines would break purely on length.
    Skipped characters are handed to the unit they follow, which is where Chinese
    punctuation belongs.
    """
    if not text or not units:
        return list(units)
    flat, offsets = _normalised(text)
    merged, cursor, flat_cursor = [], 0, 0
    for word, start, end in units:
        needle = "".join(character for character in word if character.isalnum()).lower()
        if not needle:
            if merged:
                previous, first, last = merged[-1]
                merged[-1] = (previous + word, first, last)
            continue
        found = flat.find(needle, flat_cursor)
        if found < 0:
            merged.append((word, start, end))
            continue
        begin = offsets[found]
        stop = offsets[found + len(needle) - 1] + 1
        if merged and begin > cursor:
            previous, first, last = merged[-1]
            merged[-1] = (previous + text[cursor:begin], first, last)
        piece = text[begin:stop] if merged else text[:stop]
        merged.append((piece, start, end))
        cursor, flat_cursor = stop, found + len(needle)
    if merged and cursor < len(text):
        previous, first, last = merged[-1]
        merged[-1] = (previous + text[cursor:], first, last)
    return merged


def text_atoms(text, max_chars=MAX_LINE_CHARS):
    """Break a transcript into the smallest pieces a line may start on.

    Sentence marks end an atom, a clause mark ends one once the atom is already
    long enough to read, and an atom with no marks at all is cut at
    ``max_chars`` so a run-on transcript still has somewhere to break.
    ``"".join(text_atoms(t)) == t`` always holds.
    """
    text = text or ""
    atoms, first = [], 0
    for index, character in enumerate(text):
        last = index + 1
        piece = text[first:last]
        if len(piece) >= max_chars:
            atoms.append(piece)
            first = last
        elif character in SENTENCE_END:
            atoms.append(piece)
            first = last
        elif character == PERIOD:
            following = text[last:last + 1]
            if not _is_decimal(piece, following) and not (following and not following.isspace()):
                atoms.append(piece)
                first = last
        elif character in CLAUSE_END and len(piece) >= MIN_CLAUSE_BREAK_CHARS:
            atoms.append(piece)
            first = last
    if first < len(text):
        atoms.append(text[first:])
    return atoms


def estimate_units(text, start, end):
    """Spread a span over its atoms by character share.

    Used where no alignment exists for a range — without an aligner, after a
    failed request, or for one piece of a long clip whose own alignment failed.
    The span is already known, so the estimate never drifts beyond it.
    """
    atoms = text_atoms(text)
    total = sum(len(atom) for atom in atoms) or 1
    units, cursor = [], float(start)
    for atom in atoms:
        span = (end - start) * len(atom) / total
        units.append((atom, cursor, cursor + span))
        cursor += span
    return units


def _split_units(units, width, max_seconds):
    """Break every unit that is too long to be a single line.

    A clean alignment answers one unit per character, but a degraded one — a
    long clip, or text the aligner could not match — answers one unit per
    segment. Grouping cannot break inside a unit, so a whole paragraph would
    become one 20-second line. The unit's own span is spread over its
    characters, which keeps its start and end exact.
    """
    if max_seconds is None or max_seconds <= 0:
        max_seconds = MAX_LINE_SECONDS
    result = []
    for text, start, end in units:
        pieces = 1
        if text:
            pieces = max(math.ceil(len(text) / max(1, width)),
                         math.ceil(max(0.0, end - start) / max_seconds))
            pieces = min(pieces, len(text))
        if pieces <= 1:
            result.append((text, start, end))
            continue
        for index in range(pieces):
            first = round(index * len(text) / pieces)
            last = round((index + 1) * len(text) / pieces)
            piece = text[first:last]
            if not piece:
                continue
            result.append((piece,
                           start + (end - start) * first / len(text),
                           start + (end - start) * last / len(text)))
    return result


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


def _is_sentence_end(part, following=None):
    """True when a line may not continue past ``part``."""
    stripped = (part or "").rstrip()
    if not stripped:
        return False
    core = stripped.rstrip(TRAILING)
    if core.endswith(tuple(SENTENCE_END)):
        return True
    return len(core) > 1 and core.endswith(PERIOD) and not _is_decimal(core, following)


def _is_clause_end(part):
    stripped = (part or "").rstrip()
    return bool(stripped) and stripped.rstrip(TRAILING).endswith(tuple(CLAUSE_END))


def _length_of(group):
    # Trailing spaces belong to the aligner's token boundary, not to the line: a
    # 5-character line ending in a space is still a fragment worth folding away.
    return len("".join(part for part, _, _ in group).strip())


def _blocks(units):
    """Split the stream where a line must never run on: a sentence end.

    Pauses are deliberately *not* a hard boundary. A forced aligner drifts over a
    long clip and invents a pause in the middle of a word — honouring it is what left
    "而" alone at the end of a line and "且" starting the next one. A real pause is
    still preferred, but only as a scoring bonus when a line needs cutting anyway.
    Duration is handled the same way, inside ``_split_block``, so a line that hits the
    time limit backs up to a mark instead of being guillotined where it stands.
    """
    blocks, current = [], []
    for index, (part, start, end) in enumerate(units):
        current.append((part, start, end))
        following = units[index + 1] if index + 1 < len(units) else None
        if _is_sentence_end(part, following[0] if following else None):
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _cut_score(block, index, width, length, remaining):
    """How good it is to end a line just before ``block[index]``.

    A mark beats a pause, a pause beats nothing, and everything is nudged towards
    the target width so lines stay even. This is what keeps a break off the middle
    of a word when no mark is available: characters inside a word are closer
    together than characters across a word gap.
    """
    if index <= 0 or index >= len(block):
        return None
    part, _, end = block[index - 1]
    following = block[index]
    score = 0.0
    if _is_sentence_end(part, following[0]):
        score += 10.0
    elif _is_clause_end(part):
        score += 6.0
    gap = following[1] - end
    if gap > 0:
        score += min(gap, 1.0) * 4.0
    score -= abs(length - width) / max(width, 1) * 2.0
    # Leaving a few characters stranded on a line of their own reads worse than a
    # line that is a couple of characters over the target, so a cut that would orphan
    # a fragment is penalised heavily enough to lose against an earlier mark.
    if 0 < remaining < MIN_LINE_CHARS:
        score -= 5.0
    return score


def _split_block(block, width, max_seconds):
    """Cut one block into lines, letting a line run past the width to reach a mark."""
    lines = []
    left = _length_of(block)
    while block:
        start = block[0][1]
        take, length, best, best_score, reason = 0, 0, None, None, "end"
        while take < len(block):
            part, _, end = block[take]
            trial = length + len(part)
            if trial > width + LINE_SLACK_CHARS and take > 0:
                reason = "wide"
                break
            take += 1
            length = trial
            if length >= MIN_LINE_MARK_CHARS:
                score = _cut_score(block, take, width, length, left - length)
                if score is not None and (best_score is None or score > best_score):
                    best, best_score = take, score
            if end - start >= max_seconds and take > 0:
                reason = "time"
                break
            following = block[take][0] if take < len(block) else None
            if _is_sentence_end(part, following) and length >= MIN_LINE_CHARS:
                reason = "sentence"
                break
        # A blind cut (width or duration) backs up to the best break point seen, so it
        # lands on a mark or the widest gap instead of the middle of a word.
        cut = best if reason in ("wide", "time") and best else take
        lines.append(block[:cut])
        block = block[cut:]
        left = max(0, left - _length_of(lines[-1]))
    return lines


def _can_merge(left, right, width, max_seconds):
    if not left or not right:
        return False
    if _length_of(left) + _length_of(right) > width + LINE_SLACK_CHARS:
        return False
    span = right[-1][2] - left[0][1]
    if span > max_seconds + LINE_SLACK_SECONDS:
        return False
    if _is_sentence_end(left[-1][0], right[0][0]):
        # A whole sentence keeps its own line. Only a sliver that lasts a fraction of
        # a second is folded back — that is how the aligner's "迸发。" + "出来。" is
        # repaired without also welding "第一句。" and "第二句！" together.
        return _length_of(right) <= 3 and (right[-1][2] - right[0][1]) < 0.5
    return True


def _absorb_fragments(groups, width, max_seconds):
    """Fold fragments such as "Doll。" or a runaway "嚼" into the line they belong to.

    A short line is usually the aligner splitting one phrase over a pause it invented;
    playing it alone flashes a single character on screen for 80 ms.
    """
    result = []
    for group in groups:
        if result and _length_of(group) < MIN_LINE_CHARS and _can_merge(result[-1], group, width, max_seconds):
            result[-1] = result[-1] + group
            continue
        if result and _length_of(result[-1]) < MIN_LINE_CHARS and _can_merge(result[-1], group, width, max_seconds):
            result[-1] = result[-1] + group
            continue
        result.append(group)
    return result


def build_lines(units, total_duration=None, text="", max_chars=MAX_LINE_CHARS, max_seconds=MAX_LINE_SECONDS):
    """Group time-stamped units into readable, playable lines."""
    width = _line_width(text, max_chars)
    units = _split_units(reattach_punctuation(units, text), width, max_seconds)
    groups = []
    for block in _blocks(units):
        groups.extend(_split_block(block, width, max_seconds))
    return _shape(_absorb_fragments(groups, width, max_seconds), total_duration)


def _shape(groups, total_duration):
    result, previous_end = [], 0.0
    for index, group in enumerate(groups, start=1):
        text = "".join(part for part, _, _ in group).strip()
        if not text:
            continue
        # Pieces of a long clip are aligned independently, so the tail of one can
        # overshoot the head of the next by a second. Starting the next line where the
        # previous one ended keeps playback from repeating audio it already played.
        start = max(previous_end, max(0.0, group[0][1]))
        end = max(start, group[-1][2])
        if total_duration:
            end = min(end, total_duration)
            start = min(start, end)
        result.append({"index": len(result) + 1, "start": round(start, 3), "end": round(end, 3), "text": text})
        previous_end = result[-1]["end"]
    return result


def fallback_lines(text, duration):
    """Split a plain transcript on punctuation, spreading time by character share.

    The same grouping as an aligned transcript, so a missing or failed aligner
    can no longer produce one paragraph-long line: the atoms carry the marks the
    grouping breaks on and the width and duration limits still apply.
    """
    duration = duration or 0.0
    return build_lines(estimate_units(text, 0.0, duration), duration, text=text)
