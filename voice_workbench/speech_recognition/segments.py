"""Cut a long clip into aligner-sized pieces that begin and end on quiet frames.

Why the aligner needs pieces instead of the whole file:

* The engine this app writes its config for caps a request body at 100 MB
  (``max_request_body_bytes``). A 17-minute 48 kHz stereo WAV is 195 MB, so the
  multipart alignment route closed the connection mid-upload (WinError 10054)
  and the entire transcript fell back to punctuation-estimated times.
* Forced alignment also degrades with length: the model answers one unit per
  *segment* rather than one per character, which is what turned a paragraph
  into a single 23-second line.

Pieces are cut at the quietest analysis block near their target boundary, so a
word is rarely split, and the transcript is dealt out to the pieces in
proportion to their duration — the aligner then only has to match a short span
of text to a short span of audio.

Standard library only: this runs in the desktop process, which must not import
numpy or spawn a media binary.
"""
from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

from .lines import text_atoms

# Target piece length. 45 s of 48 kHz stereo is 8.6 MB, well inside the body
# cap, and short enough for the aligner to keep character-level units.
SEGMENT_SECONDS = 45.0
# How far from the target boundary the cut may move to find a quieter block.
SEARCH_SECONDS = 6.0
# A piece shorter than this is not worth its own request.
MIN_SEGMENT_SECONDS = 8.0
# 20 ms blocks: short enough to land inside a pause between words.
BLOCK_MS = 20
# Frames per read while scanning; keeps a long clip out of memory.
READ_FRAMES = 1 << 19


def _scan(path, block_ms=BLOCK_MS):
    """Peak amplitude per block, streamed. ``None`` for a format we cannot cut.

    Only 16-bit PCM is handled: the import path writes exactly that, and a
    wrong assumption here would corrupt audio rather than merely misplace a
    timestamp.
    """
    try:
        with wave.open(str(path), "rb") as stream:
            rate, channels, width = stream.getframerate(), stream.getnchannels(), stream.getsampwidth()
            if width != 2 or channels not in (1, 2) or stream.getcomptype() != "NONE":
                return None
            frames = max(1, round(rate * block_ms / 1000))
            span = frames * channels
            peaks, read = [], 0
            while True:
                raw = stream.readframes(READ_FRAMES)
                if not raw:
                    break
                values = array.array("h", raw)
                if sys.byteorder != "little":
                    values.byteswap()
                read += len(values) // channels
                for first in range(0, len(values) - span + 1, span):
                    chunk = values[first:first + span]
                    peaks.append(max(max(chunk), -min(chunk)))
            total = stream.getnframes() / rate
        return {"peaks": peaks, "block": block_ms / 1000.0, "rate": rate,
                "channels": channels, "width": width, "total": total, "frames": read}
    except (OSError, EOFError, wave.Error):
        return None


def _quietest(peaks, first, last, target):
    """Block index nearest ``target`` among the quiet ones in ``[first, last)``."""
    if last - first < 1:
        return min(max(target, 0), max(0, len(peaks) - 1))
    window = peaks[first:last]
    floor = min(window)
    # Anything within 30 % of the window's quietest block counts as a pause.
    # Without the allowance the search would insist on one exact block and
    # drift to the window edge, splitting a word.
    allowance = max(1, int(floor * 0.3))
    best, distance = first + window.index(floor), None
    for offset, value in enumerate(window):
        if value <= floor + allowance:
            gap = abs(first + offset - target)
            if distance is None or gap < distance:
                best, distance = first + offset, gap
    return best


def _boundaries(peaks, block, total, max_seconds):
    bounds, cursor = [0.0], 0.0
    while total - cursor > max_seconds:
        target = cursor + max_seconds
        first = max(0, round((target - SEARCH_SECONDS) / block))
        last = min(len(peaks), round((target + SEARCH_SECONDS) / block) + 1)
        cut = _quietest(peaks, first, last, round(target / block)) * block
        if cut - cursor < MIN_SEGMENT_SECONDS:
            cut = target
        if total - cut < MIN_SEGMENT_SECONDS:
            break
        bounds.append(cut)
        cursor = cut
    bounds.append(total)
    return bounds


def _deal(atoms, bounds, total):
    """Give each piece the atoms whose share of the text matches its share of time."""
    texts, cursor = [], 0
    shares, running = [], 0
    for atom in atoms:
        running += len(atom)
        shares.append(running)
    count = len(bounds) - 1
    for index in range(count):
        remaining = count - index - 1
        lowest = cursor + 1
        highest = min(len(atoms) - remaining, len(atoms))
        if highest < lowest:
            highest = lowest
        target = shares[-1] * bounds[index + 1] / total if total else 0
        best = min(range(lowest, highest + 1), key=lambda k: abs(shares[k - 1] - target))
        texts.append("".join(atoms[cursor:best]))
        cursor = best
    if cursor < len(atoms):
        texts[-1] += "".join(atoms[cursor:])
    return texts


def windows(path, duration=None, max_seconds=SEGMENT_SECONDS):
    """Return ``[{"start", "end"}, ...]`` for a clip that needs cutting, else ``[]``.

    An empty list means the clip is short enough for a single request, or the audio
    is a format this module will not cut. Long clips are recognised *and* aligned on
    these same windows: a single request over a 27-minute file silently drops speech,
    and a transcript missing content cannot be mapped onto the timeline at all.
    """
    info = _scan(path)
    if not info or not info["peaks"]:
        return []
    total = float(duration or info["total"])
    if total <= max_seconds:
        return []
    bounds = _boundaries(info["peaks"], info["block"], total, max_seconds)
    if len(bounds) < 3:
        return []
    return [{"start": round(bounds[index], 3), "end": round(bounds[index + 1], 3)}
            for index in range(len(bounds) - 1)]


def plan(path, text, duration=None, max_seconds=SEGMENT_SECONDS):
    """Return ``[{"start", "end", "text"}, ...]``, or ``[]`` to align in one go.

    Used when the transcript already exists as a whole and only the timings are
    missing. Text is dealt out to the windows by its share of the duration, which is
    only right when speech density is fairly even — for a file that varies, recognise
    per window instead so the text is known to match its audio.
    """
    windows_ = windows(path, duration, max_seconds)
    if not windows_:
        return []
    atoms = text_atoms(text)
    if not atoms:
        return []
    bounds = [windows_[0]["start"]] + [window["end"] for window in windows_]
    total = bounds[-1]
    if len(atoms) < len(windows_):
        # Too little text for the planned pieces: even slices, one atom each.
        step = total / len(atoms)
        bounds = [index * step for index in range(len(atoms) + 1)]
    return [{"start": round(bounds[index], 3), "end": round(bounds[index + 1], 3),
             "text": piece}
            for index, piece in enumerate(_deal(atoms, bounds, total))]


def cut(source, start, end, target):
    """Write ``[start, end)`` of ``source`` to ``target`` as the same PCM format."""
    with wave.open(str(source), "rb") as stream:
        rate, channels, width = stream.getframerate(), stream.getnchannels(), stream.getsampwidth()
        first = max(0, round(start * rate))
        remaining = max(0, min(stream.getnframes(), round(end * rate)) - first)
        stream.setpos(first)
        with wave.open(str(target), "wb") as piece:
            piece.setnchannels(channels)
            piece.setsampwidth(width)
            piece.setframerate(rate)
            while remaining > 0:
                raw = stream.readframes(min(remaining, READ_FRAMES))
                if not raw:
                    break
                piece.writeframes(raw)
                remaining -= len(raw) // (width * channels)
    return Path(target)
