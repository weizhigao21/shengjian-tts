"""Remove the lead-in silence that generative TTS heads put in front of a take.

Every model family this app ships answers with a stretch of silence before the
first phoneme, and the amount varies per take, so a queue of clips starts
unevenly and a sentence played back sounds like it begins late. Measured over
two local libraries:

* qwen3_tts (the busiest one) -- 300 takes, median 0.62 s, p90 0.84 s, worst
  8.4 s. The head is *digital* silence here: exact zeros, not a noise floor.
* voxcpm2 -- 617 takes, median 0.055 s, p90 0.34 s, and the head carries a
  -40 dBFS hiss instead of zeros.
* index_tts2 a flat 0.30 s, kokoro 0.42 s.

The cut lives here rather than in a backend adapter because the same bytes
reach the library through three paths: the native engine's HTTP answer, the
external Python worker, and the batch worker.

Standard library only on purpose -- this runs inside the desktop process,
which must not import numpy or spawn a media binary.
"""
from __future__ import annotations

import array
import os
import sys
import wave
from pathlib import Path

# The onset threshold is deliberately above the room noise a take starts with:
# measured heads carried 0.1-0.25 s of -40 dBFS hiss before the voice, and a
# -50 dBFS threshold ended the search in that hiss (cutting only 40 ms and
# leaving the pause in place). Real speech onsets in these libraries start at
# -30 dBFS or louder.
THRESHOLD_DBFS = -35.0
# Kept in front of the detected onset so a breath or a soft consonant is not
# clipped, and so the clip does not start on a hard transient.
PADDING_MS = 30
# A linear ramp over the padding; a hard start on a non-zero sample clicks.
FADE_IN_MS = 15
# Cuts shorter than this are not worth rewriting the file for.
MIN_CUT_MS = 40
# How much may be removed. When the head is already digital silence the long
# ceiling applies -- qwen3 routinely answers with 0.6 s of exact zeros and
# occasionally several seconds, and no onset search can mistake those for
# voice. A head that carries noise keeps the short ceiling: there a large cut
# would mean the onset search ran past a quiet real opening.
CONSERVATIVE_MAX_CUT_MS = 500
LOOSE_MAX_CUT_MS = 10000
HEAD_SILENCE_DBFS = -60.0
# 10 ms analysis blocks, and how many must be loud before an onset is accepted
# (one noisy sample must not end the search).
BLOCK_MS = 10
CONFIRM_BLOCKS = 2
# Guard against loading something pathological into memory.
MAX_BYTES = 64 * 1024 * 1024


def _block_peak(values, channels, first, last):
    """Largest absolute sample in frames [first, last) across all channels."""
    start = first * channels
    stop = min(len(values), last * channels)
    if stop <= start:
        return 0
    chunk = values[start:stop]
    return max(max(chunk), -min(chunk))


def _onset_frame(values, channels, rate, threshold):
    """First frame of a sustained loud run, or None when nothing stands out."""
    block = max(1, round(rate * BLOCK_MS / 1000))
    frames = len(values) // channels
    run = 0
    for first in range(0, frames, block):
        if _block_peak(values, channels, first, first + block) >= threshold:
            run += 1
            if run >= CONFIRM_BLOCKS:
                return first - block * (CONFIRM_BLOCKS - 1)
        else:
            run = 0
    return None


def trim_leading_silence(path, *, padding_ms=PADDING_MS, fade_in_ms=FADE_IN_MS,
                         threshold_dbfs=THRESHOLD_DBFS, min_cut_ms=MIN_CUT_MS,
                         max_cut_ms=CONSERVATIVE_MAX_CUT_MS):
    """Cut the silent head of a PCM WAV in place.

    Returns ``{"trimmed": seconds, "duration": seconds}`` on a rewrite, or an
    empty dict when the file is left untouched -- a format this cannot prove
    safe, a take that already starts on the voice, or an I/O failure. Callers
    treat every failure as "keep the original audio": a bad cut would be far
    worse than a leading pause.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return {}
    if not 44 <= size <= MAX_BYTES:
        return {}
    try:
        with wave.open(str(path), "rb") as stream:
            rate, channels = stream.getframerate(), stream.getnchannels()
            width, frames = stream.getsampwidth(), stream.getnframes()
            if stream.getcomptype() != "NONE":
                return {}
            raw = stream.readframes(frames)
    except (OSError, EOFError, wave.Error):
        return {}
    # 16-bit PCM is what every backend in this project writes; anything else
    # keeps its audio rather than risk a bad re-encode.
    if width != 2 or channels < 1 or channels > 2 or len(raw) != frames * channels * width:
        return {}
    values = array.array("h", raw)
    if sys.byteorder != "little":
        values.byteswap()

    threshold = 32768.0 * 10 ** (threshold_dbfs / 20)
    peak = max(max(values), -min(values))
    # A take whose loudest moment never reaches the onset threshold has no
    # detectable voice to anchor the cut on; leave it alone.
    if peak < threshold:
        return {}
    onset = _onset_frame(values, channels, rate, threshold)
    if onset is None:
        return {}
    cut = onset - round(rate * padding_ms / 1000)
    # Whether the head is already exact zeros decides how much may go: a quiet
    # real opening must not be mistaken for a pause, but zeros cannot be voice.
    head = min(frames, round(rate * max(min_cut_ms, padding_ms) / 1000))
    head_peak = max((abs(value) for value in values[:head * channels]), default=0)
    ceiling = LOOSE_MAX_CUT_MS if head_peak <= 32768.0 * 10 ** (HEAD_SILENCE_DBFS / 20) else max_cut_ms
    if cut <= 0 or cut * 1000 / rate < min_cut_ms or cut * 1000 / rate > ceiling:
        return {}

    kept = values[cut * channels:]
    fade = round(rate * fade_in_ms / 1000)
    if fade > 0:
        limit = min(fade, len(kept) // channels)
        for i in range(limit):
            gain = (i + 1) / fade
            for c in range(channels):
                kept[i * channels + c] = round(kept[i * channels + c] * gain)
    if sys.byteorder != "little":
        kept.byteswap()

    temp = path.with_name(path.name + ".trim")
    try:
        with wave.open(str(temp), "wb") as dest:
            dest.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            dest.writeframes(kept.tobytes())
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return {}
    return {"trimmed": cut / rate, "duration": (len(kept) // channels) / rate}
