"""Long clips must reach the aligner in pieces, and no line may span a paragraph.

Regression guards for a 17-minute 48 kHz stereo clip: the multipart upload
exceeded the engine's 100 MB body cap, the connection was closed mid-request
(WinError 10054), and the transcript fell back to punctuation-estimated times —
which put 100 characters into one 23-second line.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import threading
import wave
from pathlib import Path

import httpx
import pytest

from voice_workbench.errors import VoiceError
from voice_workbench.speech_recognition import segments
from voice_workbench.speech_recognition.lines import (LINE_SLACK_CHARS, MAX_LINE_CHARS, MAX_LINE_SECONDS,
                                                      build_lines, fallback_lines, text_atoms)
from voice_workbench.speech_recognition.runner import MAX_REQUEST_BODY_BYTES, RecognitionRunner
from voice_workbench.speech_recognition.service import align_units


def speech_wav(path, seconds=100, rate=8000, silences=()):
    """A WAV whose loud blocks are 1000 and whose silent spans are exact zeros."""
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


SENTENCES = ['第%d句话在这里。' % index for index in range(1, 21)]
TEXT = ''.join(SENTENCES)  # 180 characters, 20 sentences


# ---------------------------------------------------------------------------- plan

def test_a_long_clip_is_cut_into_aligner_sized_pieces_at_quiet_points(tmp_path):
    audio = speech_wav(tmp_path / 'long.wav', 120, silences=[(41, 43), (84, 86)])
    pieces = segments.plan(audio, TEXT, 120.0)
    assert len(pieces) >= 3
    # Every piece stays inside what one request can carry and one alignment can resolve.
    assert all(piece['end'] - piece['start'] <= segments.SEGMENT_SECONDS + segments.SEARCH_SECONDS
               for piece in pieces)
    # The cut moved to the silence rather than landing on the nominal boundary.
    assert 41.0 <= pieces[0]['end'] <= 43.0
    assert 84.0 <= pieces[1]['end'] <= 86.0
    # The transcript is dealt out completely: nothing lost, nothing repeated.
    assert ''.join(piece['text'] for piece in pieces) == TEXT
    assert [piece['start'] for piece in pieces] == [0.0] + [piece['end'] for piece in pieces[:-1]]
    assert pieces[-1]['end'] == pytest.approx(120.0)


def test_a_short_clip_keeps_the_single_request_path(tmp_path):
    audio = speech_wav(tmp_path / 'short.wav', 20)
    assert segments.plan(audio, TEXT, 20.0) == []


def test_only_16_bit_pcm_is_cut_so_no_audio_is_rewritten_blindly(tmp_path):
    path = tmp_path / 'float.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 4, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00' * 4 * 8000 * 90)
    assert segments.plan(path, TEXT, 90.0) == []


def test_a_cut_piece_carries_the_same_pcm_format_as_its_source(tmp_path):
    audio = speech_wav(tmp_path / 'long.wav', 100, silences=[(44, 46)])
    pieces = segments.plan(audio, TEXT, 100.0)
    target = segments.cut(audio, pieces[0]['start'], pieces[0]['end'], tmp_path / 'piece.wav')
    with wave.open(str(target), 'rb') as piece, wave.open(str(audio), 'rb') as source:
        assert (piece.getframerate(), piece.getnchannels(), piece.getsampwidth()) == (
            source.getframerate(), source.getnchannels(), source.getsampwidth())
        assert piece.getnframes() / piece.getframerate() == pytest.approx(
            pieces[0]['end'] - pieces[0]['start'], abs=0.01)


# --------------------------------------------------------------------------- lines

def test_no_line_may_hold_a_paragraph_even_without_a_period():
    # The exact shape that produced one 23-second line: comma-only run-on text.
    source = ('Baby sleep，因为他的脑袋完全被填满了，充满着蓬松的粉红色棉花糖，不能被用来思考，'
              '而是让他盲目、空白和接受，感到非常愉快，感到愚蠢，狂妄地想要咯咯傻笑，'
              '感觉越来越愚蠢，脑袋里头都是粉色棉花糖。')
    lines = fallback_lines(source, 23.77)
    assert len(lines) > 4
    # The width is a target: a line may run past it to reach a mark, never to swallow
    # a paragraph. 24 + LINE_SLACK_CHARS is the ceiling.
    assert all(len(line['text']) <= MAX_LINE_CHARS + LINE_SLACK_CHARS for line in lines)
    assert all(line['end'] - line['start'] <= MAX_LINE_SECONDS for line in lines)
    assert ''.join(line['text'] for line in lines) == source
    assert lines[0]['start'] == 0 and lines[-1]['end'] == pytest.approx(23.77)


def test_text_without_any_punctuation_still_has_somewhere_to_break():
    source = '甲' * 60
    assert text_atoms(source) == ['甲' * 24, '甲' * 24, '甲' * 12]
    lines = fallback_lines(source, 30.0)
    assert all(len(line['text']) <= MAX_LINE_CHARS + LINE_SLACK_CHARS for line in lines)
    assert ''.join(line['text'] for line in lines) == source


def test_one_oversized_aligner_unit_is_split_instead_of_becoming_one_line():
    # What a degraded alignment returns: one unit holding a whole paragraph.
    units = [('一二三四五六七八九十十一十二十三十四十五十六十七十八十九二十二十一二十二', 10.0, 34.0)]
    lines = build_lines(units, 60.0)
    assert len(lines) > 1
    assert all(line['end'] - line['start'] <= MAX_LINE_SECONDS for line in lines)
    assert lines[0]['start'] == pytest.approx(10.0) and lines[-1]['end'] == pytest.approx(34.0)


def test_a_short_transcript_is_still_one_line():
    assert [line['text'] for line in fallback_lines('下载进度刷新，保留滚动位置。', 4.0)] == [
        '下载进度刷新，保留滚动位置。']


def _char_units(text, step=0.2, pause=None):
    """One unit per character, the shape a clean alignment returns."""
    units, clock = [], 0.0
    for index, character in enumerate(character for character in text if character not in '。！？，,、；;：: '):
        if pause and index in pause:
            clock += pause[index]
        units.append((character, clock, clock + step))
        clock += step
    return units


def test_the_width_is_a_target_so_a_word_is_never_cut_in_half():
    # "棉花糖" used to become "…粉红色棉花" + "糖，…" because 24 characters was a wall.
    source = '因为他的脑袋完全被填满了，充满着蓬松的粉红色棉花糖，不能被用来思考。'
    lines = build_lines(_char_units(source), 20.0, text=source)
    assert any('棉花糖' in line['text'] for line in lines)
    assert [line['text'] for line in lines] == [
        '因为他的脑袋完全被填满了，充满着蓬松的粉红色棉花糖，', '不能被用来思考。']


def test_an_invented_pause_between_two_characters_does_not_make_a_one_character_line():
    # The aligner drifts over a long clip and reports a 2.6 s pause after "而"; honouring
    # it left "而" alone at the end of a line with "且" starting the next one.
    source = '而且在发光的粉红色按钮上的是一个由大写字母组成的单词。'
    lines = build_lines(_char_units(source, step=0.08, pause={1: 2.6}), 20.0, text=source)
    assert [line['text'] for line in lines] == [source]
    assert all(len(line['text']) >= 6 for line in lines)


def test_a_unit_the_aligner_joined_across_a_mark_is_not_emitted_twice():
    # The aligner answered "sleepBambi" where the transcript read "sleep。Bambi", so the
    # old matcher failed, left its cursor behind, and re-emitted words already placed.
    source = 'Bambi sleep。Bambi现在可以想象。'
    units = [('Bambi', 0.0, 0.2), ('sleepBambi', 0.3, 0.6), ('现', 0.7, 0.9), ('在', 0.9, 1.1),
             ('可', 1.1, 1.3), ('以', 1.3, 1.5), ('想', 1.5, 1.7), ('象', 1.7, 1.9)]
    lines = build_lines(units, 2.0, text=source)
    assert ''.join(line['text'] for line in lines) == source


def test_a_trailing_fragment_is_folded_into_the_line_that_owns_it():
    # "Bingo" then "Doll。" used to be two lines, the second one 80 ms long.
    source = '一个甜美性感的笨蛋，一个绝望无脑的 Bingo Doll。'
    units = [('一个甜美性感的笨蛋，', 0.0, 1.2), ('一个绝望无脑的', 1.2, 2.4),
             ('Bingo', 2.4, 2.6), ('Doll。', 2.6, 2.68)]
    assert [line['text'] for line in build_lines(units, 3.0, text=source)] == [source]


def test_pieces_aligned_separately_never_make_the_next_line_start_before_the_last_end():
    # Two pieces of one clip can overlap by a second where they meet; playback must not
    # repeat audio that was already heard.
    units = [('第', 0.0, 0.5), ('一', 0.5, 1.0), ('句', 1.0, 1.5),
             ('第', 1.2, 1.6), ('二', 1.6, 2.0), ('句', 2.0, 2.4)]
    lines = build_lines(units, 2.4, text='第一句。第二句。')
    assert [line['text'] for line in lines] == ['第一句。', '第二句。']
    assert lines[1]['start'] == pytest.approx(lines[0]['end'])


# ------------------------------------------------------------------ piece alignment

class PieceRunner:
    """Answers one unit per character and records the text each request carried."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def align(self, voices, audio, text, language, cancelled):
        self.calls.append((Path(audio), text))
        if len(self.calls) in self.fail:
            raise VoiceError('ALIGN_FAILED', 'fixture: piece unavailable')
        words = [{'word': character, 'start_sample': index * 1600, 'end_sample': (index + 1) * 1600}
                 for index, character in enumerate(text)]
        return {'words': words}


def test_a_long_clip_is_aligned_once_per_piece_and_the_times_are_shifted(tmp_path):
    audio = speech_wav(tmp_path / 'long.wav', 120, silences=[(41, 43), (84, 86)])
    runner = PieceRunner()
    units, warning = align_units(runner, [], audio, TEXT, 'Chinese', 120.0, threading.Event())
    assert warning == '' and len(runner.calls) >= 3
    assert ''.join(text for _, text in runner.calls) == TEXT
    assert all(str(path).isascii() for path, _ in runner.calls)
    # A gap between pieces is a gap in the audio, never a backwards jump in time.
    assert [start for _, start, _ in units] == sorted(start for _, start, _ in units)
    assert units[-1][2] <= 120.0


def test_one_failed_piece_keeps_its_own_range_instead_of_losing_the_transcript(tmp_path):
    audio = speech_wav(tmp_path / 'long.wav', 100, silences=[(44, 46)])
    runner = PieceRunner(fail={1})
    units, warning = align_units(runner, [], audio, TEXT, 'Chinese', 100.0, threading.Event())
    assert units and '1/' in warning
    assert [start for _, start, _ in units] == sorted(start for _, start, _ in units)
    # The failed first piece still spans from zero, estimated by character share.
    assert units[0][1] == pytest.approx(0.0)
    assert units[-1][2] <= 100.0


# ---------------------------------------------------------------- transport routes

def test_an_oversized_upload_skips_the_multipart_route(tmp_path, monkeypatch):
    audio = speech_wav(tmp_path / 'big.wav', 2)
    monkeypatch.setattr('voice_workbench.speech_recognition.runner.MAX_REQUEST_BODY_BYTES', 1024)
    runner = RecognitionRunner(tmp_path / 'engine')
    monkeypatch.setattr(runner, 'start', lambda *args: (None, 'http://engine.test'))
    routes = []

    def handle(request):
        routes.append(request.url.path)
        return httpx.Response(200, json={'words': [{'word': '好', 'start_sample': 0, 'end_sample': 1600}]})

    original = httpx.Client
    monkeypatch.setattr('voice_workbench.speech_recognition.runner.httpx.Client',
                        lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    assert runner.align([], audio, '好', 'zh', threading.Event())['words']
    assert routes == ['/v1/tasks/run']
    assert MAX_REQUEST_BODY_BYTES > 1024


def test_a_closed_connection_falls_through_to_the_next_route(tmp_path, monkeypatch):
    # WinError 10054 arrived as a dropped socket, not an HTTP status, and used to
    # escape the route loop entirely.
    audio = speech_wav(tmp_path / 'clip.wav', 2)
    runner = RecognitionRunner(tmp_path / 'engine')
    monkeypatch.setattr(runner, 'start', lambda *args: (None, 'http://engine.test'))
    routes = []

    def handle(request):
        routes.append(request.url.path)
        if request.url.path == '/v1/audio/alignments':
            raise httpx.RemoteProtocolError('connection reset by peer')
        return httpx.Response(200, json={'words': [{'word': '好', 'start_sample': 0, 'end_sample': 1600}]})

    original = httpx.Client
    monkeypatch.setattr('voice_workbench.speech_recognition.runner.httpx.Client',
                        lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    assert runner.align([], audio, '好', 'zh', threading.Event())['words']
    assert routes == ['/v1/audio/alignments', '/v1/tasks/run']
