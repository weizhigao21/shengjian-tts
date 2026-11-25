"""Guards for recognising a long clip window by window.

A 27-minute English track exposed three defects at once: one request over the whole
file dropped about a third of the speech, the surviving transcript was then dealt out
to the windows by assumed speech rate and no longer matched what was said in them, and
the line splitter cut words in half at 24 characters — "keeping th" / "em", "jea" /
"ns.", "you ar" / "e wearing".

These tests pin the three fixes: text is recognised where it is spoken, windows are
aligned against their own transcript, and no split may land inside a word.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import wave
from pathlib import Path

import pytest

from voice_workbench.speech_recognition import segments
from voice_workbench.speech_recognition.lines import _split_text, build_lines, text_atoms
from voice_workbench.speech_recognition.service import (PIECEWISE_RECOGNITION_WINDOWS,
                                                        _looks_aligned, recognize_windows)


def speech_wav(path, seconds=100, rate=8000, silences=()):
    silent = set()
    for first, last in silences:
        silent.update(range(round(first * rate), round(last * rate)))
    frames = bytearray()
    for frame in range(round(seconds * rate)):
        value = (0 if frame in silent else 1000).to_bytes(2, 'little', signed=True)
        frames += value
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(bytes(frames))
    return path


ENGLISH = ('I said, "Shut in the sea, little dress." I need slutty outfits. '
           'Wear more outfits that show off your juicy bubble butt. More, more.')


# ------------------------------------------------------------------- word boundaries

def test_the_atom_splitter_never_cuts_inside_a_word():
    atoms = text_atoms(ENGLISH)
    assert ''.join(atoms) == ENGLISH
    # Every break except the last lands on a space or a mark, never mid-word.
    for atom in atoms[:-1]:
        assert atom[-1] == ' ' or atom[-1] in '。！？!?…，,、；;：:.', atom


def test_a_long_unit_is_split_between_words_not_inside_one():
    unit = ('Wear more outfits that show off your juicy bubble butt and keep them '
            'staring at your body all night long')
    lines = build_lines([(unit, 0.0, 40.0)], 40.0, text=unit)
    # Line texts are stripped, so compare with the spaces taken out as well.
    assert ''.join(line['text'] for line in lines).replace(' ', '') == unit.replace(' ', '')
    # A break is legal only where the original text has a space or a mark.
    for left, right in zip(lines, lines[1:]):
        probe = left['text'][-12:] + right['text'][:12]
        assert probe not in unit, (left['text'], right['text'])


def test_a_single_word_is_never_cut_in_half_and_never_holds_the_screen():
    lines = build_lines([('Exactly.', 0.0, 14.5)], 14.5, text='Exactly.')
    assert [line['text'] for line in lines] == ['Exactly.']
    assert lines[0]['end'] - lines[0]['start'] <= 8.0 + 0.001


def test_split_text_returns_fewer_parts_rather_than_breaking_a_word():
    assert _split_text('Exactly.', 3) == ['Exactly.']
    assert ''.join(_split_text(ENGLISH, 5)) == ENGLISH


# ---------------------------------------------------------------- window recognition

class WindowRunner:
    """Transcribes each window with its own sentence and aligns the same words.

    ``collapse`` makes the aligner answer one 80 ms frame per word, which is what a
    request that cannot match its text returns.
    """

    def __init__(self, texts=None, collapse=False, fail_align=()):
        self.texts = list(texts or [])
        self.collapse = collapse
        self.fail_align = set(fail_align)
        self.transcribed = []
        self.aligned = []

    def transcribe(self, voices, audio, language, cancelled):
        with wave.open(str(audio)) as stream:
            span = stream.getnframes() / max(1, stream.getframerate())
        text = self.texts.pop(0) if self.texts else 'window %d' % (len(self.transcribed) + 1)
        self.transcribed.append((Path(audio), span, text))
        return {'text': text, 'language': 'English'}

    def align(self, voices, audio, text, language, cancelled):
        self.aligned.append(text)
        if len(self.aligned) in self.fail_align:
            raise RuntimeError('fixture: align unavailable')
        words = text.split(' ')
        if self.collapse:
            step, span = 0.08, None
        else:
            with wave.open(str(audio)) as stream:
                span = stream.getnframes() / max(1, stream.getframerate())
            step = max(0.08, span * 0.9 / max(1, len(words)))
        return {'words': [{'word': word, 'start_sample': round(index * step * 16000),
                           'end_sample': round((index + 1) * step * 16000)}
                          for index, word in enumerate(words)]}


def window_audio(tmp_path):
    audio = speech_wav(tmp_path / 'long.wav', 150, silences=[(40, 42), (85, 87), (125, 127)])
    return audio, segments.windows(audio, 150.0)


def test_each_window_is_aligned_against_the_text_heard_in_that_window(tmp_path):
    audio, windows = window_audio(tmp_path)
    assert len(windows) >= PIECEWISE_RECOGNITION_WINDOWS
    texts = ['window number %d spoken here' % index for index in range(len(windows))]
    runner = WindowRunner(texts=list(texts))
    text, units, warning, language, _wall = recognize_windows(
        runner, [], audio, windows, 'auto', threading.Event())
    assert language == 'English'
    assert runner.aligned == texts
    assert all(body in text for body in texts)
    assert units and all(0.0 <= start <= 150.0 for _, start, _ in units)
    # Times never jump backwards, so no line can start before the previous one ended.
    assert [start for _, start, _ in units] == sorted(start for _, start, _ in units)


def test_a_word_repeated_across_a_window_seam_is_kept_once(tmp_path):
    audio, windows = window_audio(tmp_path)
    texts = ['we were walking', 'walking down the road'] + ['road was long'] * (len(windows) - 2)
    runner = WindowRunner(texts=list(texts))
    text, units, warning, language, _wall = recognize_windows(
        runner, [], audio, windows, 'English', threading.Event())
    words = [word.strip() for word, _, _ in units]
    assert words.count('walking') == 1
    assert words.count('road') == 2  # once as the seam word of window 2, once as its own


def test_a_collapsed_alignment_is_estimated_instead_of_trusted(tmp_path):
    audio, windows = window_audio(tmp_path)
    # A full sentence per window, so the collapse has enough words to be recognised.
    texts = ['this window says quite a few words out loud here'] * len(windows)
    runner = WindowRunner(texts=list(texts), collapse=True)
    text, units, warning, language, _wall = recognize_windows(
        runner, [], audio, windows, 'English', threading.Event())
    assert '估算' in warning
    lines = build_lines(units, 150.0, text=text)
    assert lines
    # 8 s is the target and 9.5 s the hard ceiling (a fragment may be folded into a
    # line that is already at the limit); the old failure was 17 to 25 seconds.
    assert max(line['end'] - line['start'] for line in lines) <= 9.5 + 0.001
    assert all(line['start'] <= line['end'] for line in lines)


def test_a_window_whose_alignment_fails_keeps_its_audio_and_its_text(tmp_path):
    audio, windows = window_audio(tmp_path)
    runner = WindowRunner(fail_align={2})
    text, units, warning, language, _wall = recognize_windows(
        runner, [], audio, windows, 'English', threading.Event())
    assert text and warning and '估算' in warning
    assert [start for _, start, _ in units] == sorted(start for _, start, _ in units)


def test_no_aligner_still_recognises_every_window_and_estimates_its_timing(tmp_path):
    audio, windows = window_audio(tmp_path)
    texts = ['window number %d spoken here' % index for index in range(len(windows))]
    runner = WindowRunner(texts=list(texts))
    text, units, warning, language, _wall = recognize_windows(
        runner, [], audio, windows, 'English', threading.Event(), align=False)
    assert runner.aligned == []
    assert '未安装强制对齐器' in warning
    assert all(body in text for body in texts)
    assert units and [start for _, start, _ in units] == sorted(start for _, start, _ in units)


def test_a_crowded_front_of_the_window_is_not_mistaken_for_a_good_alignment():
    collapsed = [('word%d' % index, index * 0.08, index * 0.08 + 0.08) for index in range(57)]
    collapsed.append(('tail', 4.56, 38.4))
    assert _looks_aligned(collapsed, 46.0) is False

    spread = [('word%d' % index, index * 0.8, index * 0.8 + 0.08) for index in range(55)]
    assert _looks_aligned(spread, 46.0) is True
