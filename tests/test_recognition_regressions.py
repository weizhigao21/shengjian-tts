"""Admission, packaged audio decoding and GUI state across asynchronous updates."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.gui import Window
from voice_workbench.speech_recognition.audio_import import import_wave
from voice_workbench.audio_editing.schemas import AudioOperation
from voice_workbench.runtime import audio_decode_command
from test_audio_editing import wav
from test_speech_recognition import FakeRecognition, FakePlayer, demo_session, setup_engine, wait_session


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])


class BlockingRecognition(FakeRecognition):
    def transcribe(self, voices, audio, language, cancelled):
        self.started.set()
        cancelled.wait(10)
        return {'text': '测试。'}


def test_rejected_import_and_retry_do_not_leave_queued_state(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path/'source.wav')
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        identity = client.post('/recognition/sessions', json={'path': str(source)}).json()['id']
        before = wait_session(client, identity)
        recognition = client.app.state.manager.recognition
        files = sorted(recognition.root.glob('*.wav'))
        monkeypatch.setattr(recognition, 'runner', BlockingRecognition(recognition.root))
        active = client.post('/recognition/sessions', json={'path': str(source)}).json()['id']
        assert recognition.runner.started.wait(2)
        try:
            assert client.post('/recognition/sessions', json={'path': str(source)}).status_code == 400
            assert client.post(f'/recognition/sessions/{identity}/retry').status_code == 400
            assert client.get(f'/recognition/sessions/{identity}').json() == before
            assert len(client.get('/recognition/sessions').json()) == 2
            assert len(list(recognition.root.glob('*.wav'))) == len(files) + 1
        finally:
            client.post(f'/recognition/sessions/{active}/cancel')


def test_parallel_imports_commit_one_session_and_clean_the_losing_copy(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', BlockingRecognition)
    source = wav(tmp_path/'source.wav')
    barrier = threading.Barrier(2)
    def prepare(source, target, **kwargs):
        details = import_wave(source, target, **kwargs)
        barrier.wait(timeout=5)
        return details
    monkeypatch.setattr('voice_workbench.speech_recognition.service.import_wave', prepare)
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post('/recognition/sessions', json={'path': str(source)}), range(2)))
        assert sorted(r.status_code for r in responses) == [201, 400]
        sessions = client.get('/recognition/sessions').json()
        try:
            assert len(sessions) == 1
            assert len(list((tmp_path/'data/recognition').glob('*.wav'))) == 1
        finally:
            client.post(f"/recognition/sessions/{sessions[0]['id']}/cancel")


def test_launch_failure_restores_retry_state_and_removes_new_import(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path/'source.wav')
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        manager = client.app.state.manager
        identity = client.post('/recognition/sessions', json={'path': str(source)}).json()['id']
        wait_session(client, identity)
        before = manager.recognition.store.get(identity)['state']
        def fail():
            raise RuntimeError('fixture: engine release failed')
        with monkeypatch.context() as failures:
            failures.setattr(manager, 'release_runners', fail)
            for path, body in [('/recognition/sessions', {'path': str(source)}),
                               (f'/recognition/sessions/{identity}/retry', None)]:
                with pytest.raises(RuntimeError, match='engine release failed'):
                    client.post(path, **({'json': body} if body else {}))
                assert manager.recognition.active is None and not manager.recognition.gpu_busy
                assert manager.recognition.store.get(identity)['state'] == before
                assert len(manager.recognition.store.recent()) == 1
                assert len(list(manager.recognition.root.glob('*.wav'))) == 1


def test_recognition_and_ai_editing_enforce_both_directions_of_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', BlockingRecognition)
    source = wav(tmp_path/'source.wav')
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        manager = client.app.state.manager
        editor = manager.audio_editor
        project = editor.import_audio(source)
        parameters = AudioOperation(source_version=project['versions'][0]['id'], operation='vc',
                                    model_path='unused.gguf', state={}).model_dump()
        editor.gpu_busy = True
        try:
            response = client.post('/recognition/sessions', json={'path': str(source)})
            assert response.status_code == 400 and response.json()['error']['code'] == 'ENGINE_BUSY'
            assert manager.recognition.store.recent() == []
        finally:
            editor.gpu_busy = False
        identity = client.post('/recognition/sessions', json={'path': str(source)}).json()['id']
        assert manager.recognition.runner.started.wait(2)
        try:
            response = client.post(f"/audio-editor/projects/{project['id']}/process", json=parameters)
            assert response.status_code == 400 and response.json()['error']['code'] == 'ENGINE_BUSY'
            assert editor.active is None and not editor.gpu_busy
            # Simple PCM rendering does not reserve the inference engine.
            ordinary = client.post(f"/audio-editor/projects/{project['id']}/process", json={**parameters, 'operation': 'render'})
            assert ordinary.status_code == 202
        finally:
            client.post(f'/recognition/sessions/{identity}/cancel')


def test_legacy_stranded_record_can_be_cancelled_or_retried(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    source = wav(tmp_path/'source.wav')
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        identity = client.post('/recognition/sessions', json={'path': str(source)}).json()['id']
        wait_session(client, identity)
        store = client.app.state.manager.recognition.store
        store.update(identity, {'status': 'queued'})
        assert client.post(f'/recognition/sessions/{identity}/cancel').json()['state']['status'] == 'cancelled'
        store.update(identity, {'status': 'queued'})
        assert client.post(f'/recognition/sessions/{identity}/retry').status_code == 200
        assert wait_session(client, identity)['state']['status'] == 'done'


FIXTURES = Path(__file__).parent/'fixtures/recognition'


@pytest.mark.parametrize('suffix', ['wav', 'flac', 'mp3', 'm4a', 'aac', 'ogg', 'opus', 'wma', 'aiff', 'aif'])
def test_bundled_qt_decodes_every_advertised_format_without_soundfile(tmp_path, suffix):
    folder = tmp_path/'中文音频'
    folder.mkdir()
    source = folder/('原录音.' + suffix)
    shutil.copyfile(FIXTURES/('tone.' + suffix), source)
    original = source.read_bytes()
    target = tmp_path/'private.wav'
    details = import_wave(source, target)
    assert .1 < details['duration'] < .35
    assert source.read_bytes() == original
    import wave
    with wave.open(str(target), 'rb') as stream:
        assert stream.getsampwidth() == 2
        assert stream.getnchannels() == 2
        assert stream.getnframes() > 0


def test_broken_audio_fails_promptly_and_cleans_the_private_copy(tmp_path):
    source = tmp_path/'broken.mp3'
    source.write_bytes(b'not an audio file')
    target = tmp_path/'private.wav'
    from voice_workbench.errors import VoiceError
    with pytest.raises(VoiceError, match='解码'):
        import_wave(source, target)
    assert not target.exists() and source.read_bytes() == b'not an audio file'


def test_decode_worker_timeout_cleans_partial_output(tmp_path, monkeypatch):
    source = tmp_path/'clip.mp3'
    source.write_bytes(b'fixture')
    target = tmp_path/'private.wav'
    class Process:
        killed = False
        def communicate(self, timeout):
            if self.killed:
                return '', ''
            target.write_bytes(b'partial output')
            raise subprocess.TimeoutExpired('fixture decoder', timeout)
        def poll(self):
            return -1 if self.killed else None
        def kill(self):
            self.killed = True
    process = Process()
    monkeypatch.setattr('voice_workbench.speech_recognition.audio_import.subprocess.Popen', lambda *a, **k: process)
    times = iter([0, 121])
    monkeypatch.setattr('voice_workbench.speech_recognition.audio_import.time.monotonic', lambda: next(times))
    from voice_workbench.errors import VoiceError
    with pytest.raises(VoiceError, match='未完成'):
        import_wave(source, target)
    assert not target.exists() and process.killed


def test_service_stop_terminates_an_in_progress_decode_and_removes_output(tmp_path, monkeypatch):
    source = tmp_path/'clip.mp3'
    source.write_bytes(b'fixture')
    target = tmp_path/'private.wav'
    cancelled = threading.Event()
    class Process:
        killed = False
        def communicate(self, timeout):
            if self.killed:
                return '', ''
            target.write_bytes(b'partial output')
            cancelled.set()
            raise subprocess.TimeoutExpired('fixture decoder', timeout)
        def poll(self):
            return -1 if self.killed else None
        def kill(self):
            self.killed = True
    process = Process()
    monkeypatch.setattr('voice_workbench.speech_recognition.audio_import.subprocess.Popen', lambda *a, **k: process)
    from voice_workbench.errors import VoiceError
    with pytest.raises(VoiceError) as error:
        import_wave(source, target, cancelled=cancelled)
    assert error.value.code == 'SERVICE_STOPPING'
    assert process.killed and not target.exists()


def test_frozen_decode_uses_the_bundled_service_without_python(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path/'声笺.exe'))
    assert audio_decode_command('source.mp3', 'target.wav') == [str(tmp_path/'声笺服务.exe'), 'decode-audio', 'source.mp3', 'target.wav']


class ControllerClient:
    def __init__(self, record):
        self.record = deepcopy(record)
        self.selection = {'language': 'zh'}
        self.retry_count = 0
        self.languages = [('自动判断', 'auto'), ('中文', 'zh'), ('英语', 'en')]

    def get(self, path):
        if path.endswith('/options'):
            body = {'asr': [{'name': 'ASR', 'family': 'qwen3_asr', 'path': 'asr.gguf'}],
                    'languages': self.languages, 'selection': self.selection, 'active': None}
        elif path.endswith('/sessions'):
            body = []
        else:
            body = deepcopy(self.record)
        return httpx.Response(200, json=body)

    def patch(self, path, **kwargs):
        self.record['state'].update(kwargs['json'])
        return httpx.Response(200, json=self.record)

    def post(self, path, **kwargs):
        if path.endswith('/selection'):
            self.selection.update(kwargs['json'])
            return httpx.Response(200, json=self.selection)
        self.retry_count += 1
        return httpx.Response(200, json=self.record)


@pytest.fixture
def page(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(Window, 'load_saved_jobs', lambda self: None)
    window = Window(18998, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop(); window.settings.timer.stop(); window.document_timer.stop()
    controller = window.recognition.controller
    controller.timer.stop()
    window.show()
    window.start_creation('recognition')
    controller.timer.stop()
    window.ready = True
    record = demo_session(tmp_path, [{'index': 1, 'start': 0., 'end': 1., 'text': '甲'},
                                     {'index': 2, 'start': 1., 'end': 2., 'text': '乙'}])
    record['state']['language'] = 'zh'
    client = ControllerClient(record)
    def call(action, success, error=None):
        try:
            result = action(client)
        except Exception as exc:
            if error:
                error(str(exc))
            else:
                raise
        else:
            success(result)
    window.call = call
    controller.options = client.get('/recognition/options').json()
    controller.session = deepcopy(record)
    controller.render_options(); controller.render_session(); controller.update_actions()
    player = controller.player
    controller.player = FakePlayer()
    yield window, controller, client
    controller.player = player
    controller.name_timer.stop()
    window.document_dirty = False
    window.close()


def test_refresh_preserves_playback_row_selection_and_continuation(page):
    _, controller, _ = page
    panel = controller.panel
    panel.lines.setCurrentRow(0)
    controller.continuous = True
    controller.play_line(0)
    before = (panel.clock.text(), panel.lines.item(0), controller.player.stops)
    controller.refresh()
    assert controller.playing_line == 0 and controller.play_range == (0., 1.)
    assert panel.lines.currentRow() == 0
    assert panel.clock.text() == before[0] and panel.lines.item(0) is before[1]
    assert controller.player.stops == before[2]
    controller.position_changed(1000)
    assert controller.playing_line == 1 and controller.play_range == (1., 2.)
    controller.play_line(1)
    assert controller.play_range is None


def test_new_results_stop_stale_playback_instead_of_reusing_old_times(page):
    _, controller, client = page
    controller.play_line(0)
    client.record['state']['lines'][0]['end'] = .5
    controller.refresh()
    assert controller.playing_line is None and controller.play_range is None
    assert '00:00.50' in controller.panel.lines.item(0).text()


def test_language_and_name_edits_survive_poll_and_persist_to_the_session(page, qtbot):
    _, controller, client = page
    panel = controller.panel
    panel.language.setCurrentIndex(panel.language.findData('en'))
    panel.title.setText('新的录音名称')
    controller.refresh()
    assert panel.language.currentData() == 'en' and panel.title.text() == '新的录音名称'
    qtbot.waitUntil(lambda: client.record['state']['language'] == 'en' and client.record['state']['name'] == '新的录音名称', timeout=2000)
    controller.refresh()
    assert panel.language.currentData() == 'en'
    assert controller.state()['language'] == 'en'


def test_successful_retry_can_be_submitted_more_than_once(page):
    _, controller, client = page
    for expected in (1, 2, 3):
        controller.recognize()
        assert client.retry_count == expected
        assert not controller.submitting
        assert controller.panel.recognize_button.isEnabled()


def test_failed_retry_clears_pending_flag_for_another_attempt(page):
    window, controller, _ = page
    def fail(action, success, error):
        error('fixture: service unavailable')
    window.call = fail
    controller.recognize()
    assert not controller.submitting and controller.panel.recognize_button.isEnabled()


def test_language_is_loaded_even_when_session_arrives_before_options(page):
    _, controller, _ = page
    controller.options = {}
    controller.panel.language.clear()
    controller.render_options(); controller.render_session()
    controller.options = {'languages': [('自动判断', 'auto'), ('中文', 'zh'), ('英语', 'en')],
                          'selection': {'language': 'auto'}}
    controller.render_options(); controller.render_session()
    assert controller.panel.language.currentData() == 'zh'
