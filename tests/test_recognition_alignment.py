"""Automatic detection must carry a real language into forced alignment."""
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import httpx
import pytest

from voice_workbench.api import create_app
from voice_workbench.errors import VoiceError
from voice_workbench.speech_recognition.runner import RecognitionRunner
from test_audio_editing import wav
from test_speech_recognition import ALIGNED, FakeRecognition, setup_engine, wait_session


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])


@pytest.mark.parametrize('requested,detected,expected', [
    ('auto', 'Chinese', 'Chinese'), ('auto', 'English', 'English'), ('auto', 'ja', 'Japanese'),
    ('zh', 'English', 'Chinese'), ('en', None, 'English'),
])
def test_service_resolves_the_language_before_calling_the_aligner(tmp_path, monkeypatch, requested, detected, expected):
    class Recognition(FakeRecognition):
        def transcribe(self, voices, audio, language, cancelled):
            return {'text': ALIGNED['text'], 'language': detected}
        def align(self, voices, audio, text, language, cancelled):
            assert language == expected and text == ALIGNED['text']
            return super().align(voices, audio, text, language, cancelled)
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', Recognition)
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        response = client.post('/recognition/sessions', json={'path': str(wav(tmp_path/'自动判断.wav')), 'language': requested})
        state = wait_session(client, response.json()['id'])['state']
        assert state['status'] == 'done' and not state['warning']
        assert state['recognized_language'] == expected
        assert client.app.state.manager.recognition.runner.calls[0][0] == 'align'


@pytest.mark.parametrize('text,language,warning', [
    ('', 'Chinese', '未识别到文字'), (None, None, '未识别到文字'),
    (ALIGNED['text'], None, '未返回语言'), (ALIGNED['text'], 'unknown', '未返回语言'),
    ('Hello.', 'Thai', '不支持语言'),
])
def test_empty_unknown_and_unsupported_languages_do_not_send_an_invalid_alignment(tmp_path, monkeypatch, text, language, warning):
    class Recognition(FakeRecognition):
        def transcribe(self, voices, audio, requested, cancelled):
            return {'text': text, 'language': language}
        def align(self, *args):
            pytest.fail('the native aligner must not receive an invalid request')
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', Recognition)
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        result = client.post('/recognition/sessions', json={'path': str(wav(tmp_path/'录音.wav'))})
        state = wait_session(client, result.json()['id'])['state']
        assert state['status'] == 'done' and state['error'] is None
        assert state['text'] == (text or '') and warning in state['warning']
        if text:
            assert state['lines'] and '估算' in state['warning']
        else:
            assert state['lines'] == []


def test_alignment_failure_keeps_the_successful_transcript_and_estimates_its_ranges(tmp_path, monkeypatch):
    class Recognition(FakeRecognition):
        def transcribe(self, *args):
            return {'text': ALIGNED['text'], 'language': 'Chinese'}
        def align(self, *args):
            raise VoiceError('ALIGN_FAILED', 'fixture: alignment unavailable')
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', Recognition)
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        result = client.post('/recognition/sessions', json={'path': str(wav(tmp_path/'录音.wav', seconds=3))})
        state = wait_session(client, result.json()['id'])['state']
        assert state['status'] == 'done' and state['text'] == ALIGNED['text']
        assert state['lines'][0]['start'] == 0 and state['lines'][-1]['end'] == pytest.approx(3)
        assert '时间对齐失败' in state['warning'] and '估算' in state['warning']
        assert not client.app.state.manager.recognition.gpu_busy


def test_cancelling_alignment_is_not_reported_as_a_completed_estimate(tmp_path, monkeypatch):
    entered = threading.Event()
    class Recognition(FakeRecognition):
        def transcribe(self, *args):
            return {'text': ALIGNED['text'], 'language': 'Chinese'}
        def align(self, voices, audio, text, language, cancelled):
            entered.set(); cancelled.wait(5)
            raise VoiceError('CANCELLED', '已取消')
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', Recognition)
    with TestClient(create_app(tmp_path/'data')) as client:
        setup_engine(tmp_path, client)
        result = client.post('/recognition/sessions', json={'path': str(wav(tmp_path/'录音.wav'))})
        identity = result.json()['id']
        assert entered.wait(2)
        client.post(f'/recognition/sessions/{identity}/cancel')
        assert wait_session(client, identity)['state']['status'] == 'cancelled'


@pytest.mark.parametrize('generic_status,expected_routes', [
    (200, ['/v1/tasks/run']), (404, ['/v1/tasks/run', '/v1/audio/transcriptions']),
    (500, ['/v1/tasks/run']),
])
def test_native_asr_retains_language_and_only_falls_back_for_a_missing_route(tmp_path, monkeypatch, generic_status, expected_routes):
    audio = wav(tmp_path/'中文录音.wav')
    runner = RecognitionRunner(tmp_path/'engine')
    monkeypatch.setattr(runner, 'start', lambda *args: (None, 'http://engine.test'))
    routes, staged_paths = [], []
    def handle(request):
        routes.append(request.url.path)
        if request.url.path == '/v1/tasks/run':
            body = json.loads(request.content)
            assert body['model'] == 'asr' and body['request']['language'] == 'Chinese'
            staged = Path(body['request']['audio']); staged_paths.append(staged)
            assert str(staged).isascii() and staged.read_bytes() == audio.read_bytes()
            return httpx.Response(generic_status, json={'text': '你好。', 'language': 'Chinese'})
        assert b'verbose_json' in request.content and b'Chinese' in request.content
        return httpx.Response(200, json={'text': '你好。'})
    original = httpx.Client
    monkeypatch.setattr('voice_workbench.speech_recognition.runner.httpx.Client',
                        lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    if generic_status == 500:
        with pytest.raises(VoiceError, match='HTTP 500'):
            runner.transcribe([], audio, 'zh', threading.Event())
    else:
        result = runner.transcribe([], audio, 'zh', threading.Event())
        assert result['text'] == '你好。'
        if generic_status == 200:
            assert result['language'] == 'Chinese'
    assert routes == expected_routes
    assert all(not path.exists() for path in staged_paths)


@pytest.mark.parametrize('text,language', [('你好。', 'auto'), ('', 'zh')])
def test_native_alignment_rejects_missing_inputs_before_starting_the_engine(tmp_path, monkeypatch, text, language):
    runner = RecognitionRunner(tmp_path/'engine')
    monkeypatch.setattr(runner, 'start', lambda *args: pytest.fail('invalid input must not load model weights'))
    with pytest.raises(VoiceError) as error:
        runner.align([], tmp_path/'not-read.wav', text, language, threading.Event())
    assert error.value.code == 'ALIGN_INPUT_MISSING'
