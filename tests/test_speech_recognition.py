"""Speech recognition: aligner timing normalisation, line shaping, service and page."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtMultimedia import QMediaPlayer

from voice_workbench.api import create_app
from voice_workbench.config import atomic_json
from voice_workbench.gui import Window
from voice_workbench.speech_recognition.runner import RecognitionRunner
from voice_workbench.speech_recognition.lines import (ALIGNER_SAMPLE_RATE, build_lines, extract_units,
                                                      fallback_lines, reattach_punctuation)
from voice_workbench.speech_recognition.schemas import SelectionRequest
from test_audio_editing import wav
from test_model_capabilities import gguf

# The exact payload shape the engine returns for POST /v1/audio/alignments. The seconds
# fields were produced from a 48 kHz clip, so they are 16000/48000 of the truth; the
# frame indices are the trustworthy field.
ALIGNED = {
    "text": "下载进度刷新，保留滚动位置。",
    "language": "zh",
    "timing": {"wall_ms": 318.7, "audio_duration_ms": 4960.0, "rtf": 0.064},
    "words": [
        {"word": "下", "start": 0.106667, "end": 0.16, "start_sample": 5120, "end_sample": 7680, "confidence": 0},
        {"word": "载", "start": 0.16, "end": 0.213333, "start_sample": 7680, "end_sample": 10240, "confidence": 0},
        {"word": "进", "start": 0.213333, "end": 0.266667, "start_sample": 10240, "end_sample": 12800, "confidence": 0},
        {"word": "度", "start": 0.266667, "end": 0.32, "start_sample": 12800, "end_sample": 15360, "confidence": 0},
        {"word": "刷", "start": 0.32, "end": 0.373333, "start_sample": 15360, "end_sample": 17920, "confidence": 0},
        {"word": "新", "start": 0.373333, "end": 0.48, "start_sample": 17920, "end_sample": 23040, "confidence": 0},
        {"word": "保", "start": 0.48, "end": 0.533333, "start_sample": 23040, "end_sample": 25600, "confidence": 0},
        {"word": "留", "start": 0.533333, "end": 0.586667, "start_sample": 25600, "end_sample": 28160, "confidence": 0},
        {"word": "滚", "start": 0.586667, "end": 0.64, "start_sample": 28160, "end_sample": 30720, "confidence": 0},
        {"word": "动", "start": 0.64, "end": 0.693333, "start_sample": 30720, "end_sample": 33280, "confidence": 0},
        {"word": "位", "start": 0.693333, "end": 0.746667, "start_sample": 33280, "end_sample": 35840, "confidence": 0},
        {"word": "置", "start": 0.746667, "end": 0.853333, "start_sample": 35840, "end_sample": 40960, "confidence": 0},
    ],
}


class FakeRecognition:
    """Stands in for RecognitionRunner; records the engine contract it was handed."""

    def __init__(self, root):
        self.calls = []
        self.started = threading.Event()
        self.interrupts = 0

    def transcribe(self, voices, audio, language, cancelled):
        self.calls.append(("asr", audio, language, [voice['task'] for voice in voices]))
        self.started.set()
        return {"text": ALIGNED["text"], "timing": dict(ALIGNED["timing"])}

    def align(self, voices, audio, text, language, cancelled):
        self.calls.append(("align", text, language))
        return dict(ALIGNED)

    def interrupt(self):
        self.interrupts += 1


class FakePlayer:
    """Records transport calls so a clicked line can be asserted without an audio device."""

    def __init__(self):
        self.loaded = []
        self.positions = []
        self.plays = 0
        self.stops = 0

    def source(self):
        return self.loaded[-1] if self.loaded else None

    def setSource(self, source):
        self.loaded.append(source)

    def setPosition(self, value):
        self.positions.append(value)

    def play(self):
        self.plays += 1

    def stop(self):
        self.stops += 1

    def playbackState(self):
        return QMediaPlayer.PlaybackState.StoppedState


def setup_engine(tmp_path, client, families=('qwen3_asr', 'qwen3_forced_aligner')):
    """A fake engine whose model_specs declare the requested recognition tasks."""
    manager = client.app.state.manager
    engine = tmp_path / 'engine' / 'audiocpp_server.exe'
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_bytes(b'fixture')
    specs = engine.parent / 'model_specs'
    specs.mkdir(exist_ok=True)
    for family in families:
        tasks = ['align'] if 'aligner' in family else ['asr']
        (specs / f'{family}.json').write_text(json.dumps({'family': family, 'tasks': tasks}), encoding='utf-8')
    atomic_json(manager.config.path, {**manager.config.read(), 'native_engine': str(engine)})
    imported = []
    for family, name in (('qwen3_asr', 'asr.gguf'), ('qwen3_forced_aligner', 'aligner.gguf')):
        if family not in families:
            continue
        path = tmp_path / name
        gguf(path, {'family': family})
        response = client.post('/models/import', json={'path': str(path)})
        assert response.status_code == 201, response.text
        imported.append(response.json())
    return engine, imported


def wait_session(client, identity, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = client.get(f'/recognition/sessions/{identity}').json()
        if session['state']['status'] not in ('queued', 'running'):
            return session
        time.sleep(0.01)
    raise AssertionError('recognition did not finish')


# --------------------------------------------------------------------------- timings

def test_aligner_frame_indices_are_16khz_and_the_seconds_field_is_ignored():
    units = extract_units(ALIGNED)
    assert [unit[0] for unit in units] == list('下载进度刷新保留滚动位置')
    # The seconds field says 0.107 for the first character because the engine divided a
    # 16 kHz frame index by the clip's 48 kHz rate. The frame index is the truth.
    assert units[0] == ('下', 5120 / ALIGNER_SAMPLE_RATE, 7680 / ALIGNER_SAMPLE_RATE)
    assert units[0][1] == pytest.approx(0.32)
    assert units[-1][2] == pytest.approx(2.56)
    assert ALIGNED['words'][0]['start'] == pytest.approx(0.106667)
    assert units[0][1] != pytest.approx(ALIGNED['words'][0]['start'])


def test_seconds_are_used_only_when_frame_indices_are_absent():
    payload = {"words": [{"word": "甲", "start": 1.5, "end": 2.25}, {"word": "乙", "start": 2.25, "end": 3.0},
                         {"word": "丙", "start": 3.0, "end": 4.5}]}
    assert extract_units(payload) == [('甲', 1.5, 2.25), ('乙', 2.25, 3.0), ('丙', 3.0, 4.5)]
    # Other providers report milliseconds; nothing plausible as seconds exceeds a day.
    milliseconds = {"words": [{"word": "甲", "start": 92000, "end": 93500}]}
    assert extract_units(milliseconds) == [('甲', 92.0, 93.5)]
    # Frame indices always win over the seconds field, which the engine derives wrongly.
    both = {"words": [{"word": "甲", "start": 0.107, "end": 0.16, "start_sample": 5120, "end_sample": 7680}]}
    assert extract_units(both) == [('甲', 0.32, 0.48)]


def test_unknown_payload_shapes_yield_no_units_instead_of_guessing():
    assert extract_units({"text": "只有文本"}) == []
    assert extract_units({"segments": [{"text": "", "start": 0, "end": 1}]}) == []
    assert extract_units(None) == []


def test_aligner_omitted_punctuation_is_restored_so_lines_follow_sentences():
    units = extract_units(ALIGNED)
    assert '，' not in ''.join(unit[0] for unit in units)
    restored = reattach_punctuation(units, ALIGNED['text'])
    assert ''.join(unit[0] for unit in restored) == ALIGNED['text']
    assert restored[5][0] == '新，'
    assert restored[11][0] == '置。'
    assert build_lines(units, 4.96, text=ALIGNED['text']) == [
        {"index": 1, "start": 0.32, "end": 2.56, "text": ALIGNED['text']}]
    # Without the transcript there is no punctuation to restore; the line is only limited
    # by length, which documents why build_lines takes the text.
    assert build_lines(units, 4.96)[0]['text'] == ''.join(unit[0] for unit in units)


def test_lines_break_at_sentences_and_only_split_long_clauses():
    def shaped(source):
        words = [character for character in source if character not in '。，！？']
        units = [(character, index * 0.2, index * 0.2 + 0.2) for index, character in enumerate(words)]
        return build_lines(units, 20.0, text=source)

    assert [line['text'] for line in shaped('第一句。第二句！')] == ['第一句。', '第二句！']
    assert [line['index'] for line in shaped('第一句。第二句！')] == [1, 2]
    # A clause mark only ends a line once that line is long enough to read on its own.
    assert [line['text'] for line in shaped('短，还是同一行。')] == ['短，还是同一行。']
    assert [line['text'] for line in shaped('一二三四五六七八九十十一十二十三十四，后面。')] == [
        '一二三四五六七八九十十一十二十三十四，', '后面。']
    # Every line stays inside the clip even if the aligner overshoots the end.
    assert shaped('第一句。第二句！')[-1]['end'] == 1.2
    assert [line['end'] for line in build_lines([(c, i * 0.2, i * 0.2 + 1.0)
                                                for i, c in enumerate('甲乙丙')], 0.5)][0] == 0.5


def english_units(source):
    """Units shaped as the aligner returns English: words only, marks dropped."""
    clock, units = 0.0, []
    for word in ''.join(character for character in source if character not in '.,').split():
        units.append((word, clock, clock + 0.2))
        clock += 0.2
    return units


def test_english_sentences_break_on_the_period_the_aligner_drops():
    source = 'Hello there. Goodbye now.'
    lines = build_lines(english_units(source), 60.0, text=source)
    assert [line['text'] for line in lines] == ['Hello there.', 'Goodbye now.']


def test_a_decimal_point_does_not_split_a_number_across_lines():
    source = 'It costs 3.5 dollars today.'
    # A number the aligner breaks at its point: the mark lands on "3", not "5".
    units = [('It', 0.0, 0.2), ('costs', 0.2, 0.4), ('3', 0.4, 0.6), ('5', 0.6, 0.8),
             ('dollars', 0.8, 1.0), ('today', 1.0, 1.2)]
    lines = build_lines(units, 60.0, text=source)
    assert [line['text'] for line in lines] == ['It costs 3.5 dollars today.']


def test_space_separated_transcripts_get_a_wider_line_than_chinese():
    english = 'The quick brown fox jumps over a dog'
    assert 24 < len(english) <= 42
    # One line for English; the 24-character Chinese default would have wrapped it.
    assert [line['text'] for line in build_lines(english_units(english), 60.0, text=english)] == [english]
    # Chinese keeps the narrower width, so the same character budget still wraps.
    chinese = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥一二三四五六七八'
    units = [(character, index * 0.2, index * 0.2 + 0.2) for index, character in enumerate(chinese)]
    assert len(build_lines(units, 60.0, text=chinese)) == 2


def test_estimated_lines_spread_time_by_character_share():
    lines = fallback_lines('第一句。第二句话更长一些。', 12.0)
    assert [line['text'] for line in lines] == ['第一句。', '第二句话更长一些。']
    assert lines[0]['start'] == 0 and lines[1]['start'] == pytest.approx(lines[0]['end'])
    assert lines[-1]['end'] == pytest.approx(12.0)
    assert fallback_lines('', 12.0) == []
    # Without an aligner the transcript still splits on the English period.
    assert [line['text'] for line in fallback_lines('First one. Second is longer.', 4.0)] == [
        'First one.', 'Second is longer.']


# --------------------------------------------------------------------------- service

def test_import_recognises_and_keeps_a_playable_range_per_line(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path / '原文.wav', seconds=5)
    with TestClient(create_app(tmp_path / 'data')) as client:
        setup_engine(tmp_path, client)
        response = client.post('/recognition/sessions', json={'path': str(source), 'language': 'zh'})
        assert response.status_code == 201, response.text
        session = wait_session(client, response.json()['id'])
        state = session['state']
        assert state['status'] == 'done' and state['error'] is None
        assert state['text'] == ALIGNED['text'] and state['warning'] == ''
        assert state['lines'] == [{"index": 1, "start": 0.32, "end": 2.56, "text": ALIGNED['text']}]
        assert state['duration'] == pytest.approx(5.0)
        runner = client.app.state.manager.recognition.runner
        assert [call[0] for call in runner.calls] == ['asr', 'align']
        # One engine process holds both models so the aligner can follow the transcript.
        assert runner.calls[0][3] == ['asr', 'align'] and runner.calls[1][1] == ALIGNED['text']
        assert client.get(state['audio_url']).status_code == 200
        assert not client.app.state.manager.recognition.gpu_busy
        assert client.get('/health').json()['recognition'] is None


def test_terminal_state_never_lands_while_the_engine_still_looks_busy(tmp_path, monkeypatch):
    """A finished session must never be visible before the engine stops looking busy.

    The page polls the session and ``/health`` independently; if a terminal status
    landed first the two would disagree, and the next request could be refused with a
    busy error for a run that has already finished.
    """
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path / '原文.wav', seconds=2)
    with TestClient(create_app(tmp_path / 'data')) as client:
        setup_engine(tmp_path, client)
        manager = client.app.state.manager
        recognition = manager.recognition
        observed = []
        original = recognition.store.update

        def watch(identity, changes):
            if changes.get('status') in ('done', 'failed', 'cancelled'):
                observed.append((changes['status'], recognition.gpu_busy, recognition.active, manager.editing_busy))
            return original(identity, changes)

        monkeypatch.setattr(recognition.store, 'update', watch)
        identity = client.post('/recognition/sessions', json={'path': str(source)}).json()['id']
        assert wait_session(client, identity)['state']['status'] == 'done'
        assert observed, 'the terminal write should have been observed'
        for status, busy, active, editing in observed:
            assert not busy and not editing and active is None, (status, busy, active, editing)


def test_recognition_requires_an_asr_model_before_importing(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path / '原文.wav')
    with TestClient(create_app(tmp_path / 'data')) as client:
        setup_engine(tmp_path, client, families=('qwen3_forced_aligner',))
        options = client.get('/recognition/options').json()
        assert options['asr'] == [] and [model['path'] for model in options['align']]
        assert options['runtime'].endswith('audiocpp_server.exe')
        # Refused before a session or an audio copy is made, so nothing to clean up.
        assert client.post('/recognition/sessions', json={'path': str(source)}).status_code == 400
        assert client.get('/recognition/sessions').json() == []
        assert list((tmp_path / 'data' / 'recognition').glob('*.wav')) == []


def test_missing_aligner_estimates_line_times_and_reports_it(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path / '原文.wav', seconds=4)
    with TestClient(create_app(tmp_path / 'data')) as client:
        setup_engine(tmp_path, client, families=('qwen3_asr',))
        session = wait_session(client, client.post('/recognition/sessions',
                                                  json={'path': str(source)}).json()['id'])
        state = session['state']
        # Without an aligner the transcript is split on sentence marks and its duration is
        # spread by character share, so every line is still clickable.
        assert [line['text'] for line in state['lines']] == ['下载进度刷新，保留滚动位置。']
        assert state['lines'][0]['start'] == 0.0
        assert state['lines'][0]['end'] == pytest.approx(4.0)
        assert '未安装强制对齐器' in state['warning']
        runner = client.app.state.manager.recognition.runner
        assert [call[0] for call in runner.calls] == ['asr']


def test_retry_cancel_and_remove_keep_the_imported_audio_intact(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path / '原文.wav', seconds=1)
    original = source.read_bytes()
    with TestClient(create_app(tmp_path / 'data')) as client:
        setup_engine(tmp_path, client)
        identity = client.post('/recognition/sessions', json={'path': str(source)}).json()['id']
        state = wait_session(client, identity)['state']
        assert client.post(f'/recognition/sessions/{identity}/retry').status_code == 200
        assert wait_session(client, identity)['state']['status'] == 'done'
        assert client.delete(f'/recognition/sessions/{identity}').json() == {'removed': identity}
        assert client.get(f'/recognition/sessions/{identity}').status_code == 404
        assert client.get(f"/recognition/audio/{state['audio']}").status_code == 404
        listed = client.get('/recognition/sessions').json()
        assert listed == []
        # Importing copies the file; the user's own audio is never touched.
        assert source.read_bytes() == original


def test_recognition_rejects_unreadable_and_unsupported_imports(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    with TestClient(create_app(tmp_path / 'data')) as client:
        setup_engine(tmp_path, client)
        assert client.post('/recognition/sessions', json={'path': str(tmp_path / 'missing.wav')}).status_code == 400
        document = tmp_path / 'note.txt'
        document.write_text('不是音频', encoding='utf-8')
        assert client.post('/recognition/sessions', json={'path': str(document)}).status_code == 400
        broken = tmp_path / 'broken.wav'
        broken.write_bytes(b'not a wave file')
        assert client.post('/recognition/sessions', json={'path': str(broken)}).status_code == 400
        assert client.get('/recognition/sessions/does-not-exist').status_code == 404
        assert client.get('/recognition/audio/not-a-hash.wav').status_code == 404
        assert client.get('/recognition/sessions').json() == []
        assert client.post('/recognition/selection', json={'language': 'zh', 'extra': 1}).status_code == 422


def test_selection_remembers_the_chosen_models_and_language(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    with TestClient(create_app(tmp_path / 'data')) as client:
        engine, imported = setup_engine(tmp_path, client)
        aligner = str(tmp_path / 'aligner.gguf')
        selection = client.post('/recognition/selection', json={
            'asr_model': str(tmp_path / 'asr.gguf'), 'align_model': aligner, 'language': 'en'}).json()
        assert selection['asr_model'] == str(tmp_path / 'asr.gguf')
        assert selection['align_model'] == aligner and selection['language'] == 'en'
        # A null field leaves the stored choice alone.
        assert client.post('/recognition/selection', json={'language': 'yue'}).json()['asr_model'] == str(tmp_path / 'asr.gguf')
        # A model that has since been removed is dropped rather than kept as a dead path.
        (tmp_path / 'aligner.gguf').unlink()
        recovered = client.post('/recognition/selection', json={}).json()
        assert 'align_model' not in recovered and recovered['language'] == 'yue'
        assert recovered['asr_model'] == str(tmp_path / 'asr.gguf')


def test_selection_request_forbids_unknown_fields():
    with pytest.raises(ValueError):
        SelectionRequest(language='zh', model='x')
    assert SelectionRequest().model_dump() == {'asr_model': None, 'align_model': None, 'language': None}


# ------------------------------------------------------------------- model catalog

def test_asr_and_aligner_ggufs_register_for_recognition_without_a_voice_preset(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    with TestClient(create_app(tmp_path / 'data')) as client:
        setup_engine(tmp_path, client)
        installed = {model['family']: model for model in client.get('/models').json()['installed']}
        assert installed['qwen3_asr']['recognition_adapted'] and not installed['qwen3_asr']['align_adapted']
        assert installed['qwen3_forced_aligner']['align_adapted']
        assert installed['qwen3_forced_aligner']['recognition_adapted'] is False
        for family in ('qwen3_asr', 'qwen3_forced_aligner'):
            assert installed[family]['preset_count'] == 0 and installed[family]['tasks']
            assert installed[family]['edit_adapted'] is False
        assert installed['qwen3_asr']['description'].startswith('语音识别模式')
        # Recognition models must never appear as a selectable voice.
        assert all((voice.get('family') or '') not in ('qwen3_asr', 'qwen3_forced_aligner')
                   for voice in client.get('/voices').json())


def test_engine_declaration_gates_recognition_registration(tmp_path):
    with TestClient(create_app(tmp_path / 'data')) as client:
        manager = client.app.state.manager
        engine = tmp_path / 'engine' / 'audiocpp_server.exe'
        engine.parent.mkdir(parents=True, exist_ok=True)
        engine.write_bytes(b'fixture')
        atomic_json(manager.config.path, {**manager.config.read(), 'native_engine': str(engine)})
        # No model_specs at all: the engine declares no tasks, so this stays a mismatch.
        model = tmp_path / 'asr.gguf'
        gguf(model, {'family': 'qwen3_asr'})
        response = client.post('/models/import', json={'path': str(model)})
        assert response.status_code == 400 and '语音模型家族' in response.text
        specs = engine.parent / 'model_specs'
        specs.mkdir(exist_ok=True)
        (specs / 'qwen3_asr.json').write_text(json.dumps({'family': 'qwen3_asr', 'tasks': ['asr']}), encoding='utf-8')
        assert client.post('/models/import', json={'path': str(model)}).status_code == 201
        assert client.get('/recognition/options').json()['asr'][0]['family'] == 'qwen3_asr'


# --------------------------------------------------------------------------- engine

def test_recognition_engine_serves_both_models_from_ascii_paths(tmp_path, monkeypatch):
    """One process hosts the ASR and the aligner; both model paths must be ASCII.

    The native loader canonicalises junctions back to the real (Unicode) path, so a
    model under a non-ASCII directory has to be hard-linked into an ASCII alias — the
    regression this guards is handing the raw ``model_dir`` straight to the engine.
    """
    engine = tmp_path / 'engine' / 'audiocpp_server.exe'
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_bytes(b'fixture')
    folder = tmp_path / '语料库'          # deliberately non-ASCII, like a user's data dir
    folder.mkdir()
    asr_model, align_model = folder / 'asr.gguf', folder / '对齐器.gguf'
    for model in (asr_model, align_model):
        model.write_bytes(b'GGUF' + b'\0' * 8)

    configs, processes = [], []

    class Process:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1

        def wait(self, **kwargs):
            return self.returncode

    def spawn(command, **kwargs):
        configs.append(json.loads(Path(command[command.index('--config') + 1]).read_text(encoding='utf-8')))
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr('voice_workbench.audio_cpp.subprocess.Popen', spawn)
    seen = {}

    def handler(request):
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok'})
        if request.url.path == '/v1/audio/alignments':     # force the generic-task fallback
            return httpx.Response(404, text='no such route')
        if request.url.path == '/v1/tasks/run':
            seen['body'] = json.loads(request.content)
            return httpx.Response(200, json={'words': []})
        return httpx.Response(404)

    original = httpx.Client
    monkeypatch.setattr('voice_workbench.audio_cpp.httpx.Client',
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))

    staged = tmp_path / 'stage'                            # the aligner stages audio for the native reader
    staged.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(staged))

    def voice(family, model, task):
        return {'family': family, 'model_dir': str(model), 'engine_executable': str(engine),
                'device': 'cpu', 'timeout': 2, 'task': task}

    voices = [voice('qwen3_asr', asr_model, 'asr'), voice('qwen3_forced_aligner', align_model, 'align')]
    runner = RecognitionRunner(tmp_path / 'runner')
    try:
        runner.start(voices, threading.Event())
        assert len(configs) == 1, 'one process should host both models, not one per task'
        config = configs[-1]
        assert config['max_loaded_models'] == 2 and config['lazy_load'] is True
        assert [entry['task'] for entry in config['models']] == ['asr', 'align']
        for entry, model in zip(config['models'], (asr_model, align_model)):
            assert entry['path'].isascii(), 'the native reader cannot open a Unicode path'
            assert entry['path'] != str(model)
            assert Path(entry['path']).samefile(model), 'the alias must point at the same weights'

        # The latter half of the contract: language rides inside request on the fallback route.
        audio = tmp_path / 'voice.wav'
        audio.write_bytes(b'RIFF' + b'\0' * 8)
        runner.align(voices, audio, '你好，世界', 'zh', threading.Event())
        body = seen['body']
        assert body['model'] == 'align'
        assert body['request']['text'] == '你好，世界'
        assert body['request']['language'] == 'zh'
        assert body['request']['audio'].isascii() and 'options' not in body['request']
    finally:
        runner.interrupt()          # also releases the ASCII aliases it hard-linked


# ------------------------------------------------------------------------------- page

def demo_session(tmp_path, lines, name='演示录音', stem='a'):
    """A finished session whose audio really exists beside the recognition database."""
    audio = tmp_path / 'recognition' / (stem * 32 + '.wav')
    audio.parent.mkdir(parents=True, exist_ok=True)
    wav(audio, seconds=3)
    return {'id': 's1', 'created': 0, 'updated': 0, 'state': {
        'name': name, 'audio': audio.name, 'duration': 3.0, 'status': 'done', 'warning': '',
        'text': '下载进度刷新，保留滚动位置。', 'audio_url': f'/recognition/audio/{audio.name}',
        'lines': lines}}


def test_recognition_page_takes_drops_and_plays_the_clicked_line(qtbot, tmp_path):
    window = Window(18890, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window.start_creation('recognition')
    assert window.creation_pages.currentIndex() == 3
    assert window.transport.isHidden()
    panel, controller = window.recognition, window.recognition.controller
    assert not panel.drop_zone.isHidden() and panel.lines.isHidden()
    assert panel.title.text() == '未命名音频'

    controller.session = demo_session(tmp_path, [
        {'index': 1, 'start': 0.32, 'end': 1.2, 'text': '下载进度刷新，'},
        {'index': 2, 'start': 1.4, 'end': 2.56, 'text': '保留滚动位置。'}])
    controller.render_session()
    controller.update_actions()
    assert panel.title.text() == '演示录音'
    assert panel.lines.count() == 2 and panel.drop_zone.isHidden() and not panel.lines.isHidden()
    assert '[00:00.32 – 00:01.20]' in panel.lines.item(0).text()
    assert '2 行' in panel.status.text()
    assert panel.export_button.isEnabled() and panel.toggle.isEnabled() and panel.copy_button.isEnabled()

    player = FakePlayer()
    controller.player = player
    controller.play_line(1)
    assert controller.play_range == (1.4, 2.56) and controller.playing_line == 1
    assert '▶' in panel.lines.item(1).text() and '▶' not in panel.lines.item(0).text()
    assert len(player.loaded) == 1 and not player.positions  # the seek waits for the media
    assert '1.40 – 2.56' in panel.clock.text()
    controller.media_ready(QMediaPlayer.MediaStatus.LoadedMedia)
    assert player.positions == [1400]
    controller.play_line(1)  # clicking the playing line stops it
    assert controller.play_range is None and controller.playing_line is None
    assert '▶' not in panel.lines.item(1).text()

    controller.play_line(0)
    controller.position_changed(1200)
    assert controller.play_range is None
    assert player.stops >= 1
    window.close()


def test_recognition_page_advances_to_the_next_line_only_when_asked(qtbot, tmp_path):
    window = Window(18893, tmp_path, autostart=False)
    qtbot.addWidget(window)
    controller = window.recognition.controller
    controller.session = demo_session(tmp_path, [
        {'index': 1, 'start': 0.0, 'end': 1.0, 'text': '甲'},
        {'index': 2, 'start': 1.0, 'end': 2.0, 'text': '乙'}], name='连续播放', stem='b')
    controller.render_session()
    player = FakePlayer()
    controller.player = player
    controller.play_line(0)
    controller.position_changed(1000)
    assert controller.play_range is None and controller.playing_line is None
    controller.play_line(0)
    controller.continuous = True
    controller.position_changed(1000)
    assert controller.playing_line == 1 and controller.play_range == (1.0, 2.0)
    controller.position_changed(2000)
    assert controller.play_range is None and controller.playing_line is None
    window.close()


def test_recognition_page_only_imports_supported_audio_drops(qtbot, tmp_path):
    window = Window(18891, tmp_path, autostart=False)
    qtbot.addWidget(window)
    controller = window.recognition.controller
    document = tmp_path / 'note.txt'
    document.write_text('不是音频', encoding='utf-8')
    dropped = QMimeData()
    dropped.setUrls([QUrl.fromLocalFile(str(document))])
    assert not controller.accepts(dropped)
    clip = wav(tmp_path / '录音.WAV', seconds=0.2)
    dropped.setUrls([QUrl.fromLocalFile(str(document)), QUrl.fromLocalFile(str(clip))])
    assert Path(controller.dropped_path(dropped)) == clip
    assert controller.accepts(dropped)
    controller.session = None
    controller.render_details()
    assert '拖到下方区域' in window.recognition.info.text()
    window.close()


def test_creation_home_offers_recognition_beside_the_other_modes(qtbot, tmp_path):
    window = Window(18892, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.show()
    window.open_creation_home()
    assert set(window.creation_choices) == {'article', 'dialogue', 'audio', 'recognition'}
    assert '语音识别' in window.creation_choices['recognition'].text()
    window.start_creation('recognition')
    window.open_creation_home()
    assert window.creation_pages.currentIndex() == 0
    window.resume_creation_editor()
    assert window.creation_pages.currentIndex() == 1 and window.transport.isVisible()
    window.close()
