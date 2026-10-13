import array
import json
import shutil
import threading
import time
import wave
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voice_workbench.api import create_app
from voice_workbench.audio_editing.pcm import inspect_wave, render_wave
from voice_workbench.audio_editing.schemas import AudioOperation
from voice_workbench.audio_editing.vevo2 import VevoRunner, native_request
from voice_workbench.config import atomic_json
from voice_workbench.errors import VoiceError
from test_model_capabilities import gguf


def wav(path, seconds=2, channels=1, width=2, rate=8000):
    frame = (bytes([128 + 20]) if width == 1 else (1000).to_bytes(width, 'little', signed=True)) * channels
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(frame * round(rate * seconds))
    return path


def wait_version(client, identity, status='done'):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        project = client.get(f'/audio-editor/projects/{identity}').json()
        if project['versions'][0]['status'] not in ('queued', 'running'):
            assert project['versions'][0]['status'] == status, project
            return project
        time.sleep(0.01)
    raise AssertionError('edit did not finish')


@pytest.mark.parametrize('width,channels', [(1, 1), (2, 1), (2, 2), (3, 2), (4, 1)])
def test_pcm_trim_preserves_selected_frames_and_original(tmp_path, width, channels):
    source = wav(tmp_path/'original.wav', channels=channels, width=width)
    original = source.read_bytes()
    output = tmp_path/'trim.wav'
    result = render_wave(source, output, start=0.25, end=1.75)
    assert result['frames'] == 12000 and result['sample_width'] == width
    with wave.open(str(source), 'rb') as before, wave.open(str(output), 'rb') as after:
        before.setpos(2000)
        assert after.readframes(12000) == before.readframes(12000)
    assert source.read_bytes() == original


def test_gain_and_fades_apply_to_both_channels_without_clipping(tmp_path):
    source = wav(tmp_path/'original.wav', channels=2)
    result = render_wave(source, tmp_path/'gain.wav', gain_db=-6, fade_in=0.2, fade_out=0.2)
    with wave.open(str(tmp_path/'gain.wav'), 'rb') as stream:
        values = array.array('h', stream.readframes(stream.getnframes()))
    assert values[0:2] == array.array('h', [0, 0])
    assert values[-2:] == array.array('h', [0, 0])
    assert 500 <= values[8000] <= 502
    assert values[8000] == values[8001]
    assert not result['clipped_samples']


def test_append_preserves_payload_and_rejects_incompatible_format(tmp_path):
    source = wav(tmp_path/'original.wav', seconds=1)
    extra = wav(tmp_path/'extra.wav', seconds=0.5)
    result = render_wave(source, tmp_path/'joined.wav', append=extra)
    assert result['duration'] == 1.5
    incompatible = wav(tmp_path/'stereo.wav', channels=2)
    with pytest.raises(VoiceError, match='声道'):
        render_wave(source, tmp_path/'bad.wav', append=incompatible)


def test_invalid_selection_truncated_audio_and_nan_are_rejected(tmp_path):
    source = wav(tmp_path/'original.wav')
    with pytest.raises(VoiceError, match='选区'):
        render_wave(source, tmp_path/'bad.wav', start=2, end=1)
    with pytest.raises(ValidationError):
        AudioOperation(source_version='a', start=float('nan'), state={'name': 'test'})
    source.write_bytes(source.read_bytes()[:-10])
    with pytest.raises(VoiceError, match='不完整'):
        inspect_wave(source)


def test_api_versions_survive_restart_and_reject_cross_project_source(tmp_path):
    source = wav(tmp_path/'原音频.wav')
    original = source.read_bytes()
    root = tmp_path/'data'
    with TestClient(create_app(root)) as client:
        imported = client.post('/audio-editor/projects', json={'path': str(source), 'original_text': '原文'}).json()
        identity = imported['id']
        original_id = imported['versions'][0]['id']
        clip_id = imported['clip_id']
        response = client.post(f'/audio-editor/projects/{identity}/process', json={
            'source_version': original_id, 'start': 0.5, 'end': 1.5, 'gain_db': -3,
            'state': {**imported['state'], 'name': '片段一'}})
        assert response.status_code == 202
        project = wait_version(client, identity)
        result = project['versions'][0]
        assert result['parent_id'] == original_id and result['details']['duration'] == 1
        assert project['clip_id'] == clip_id and project['state']['name'] == '片段一'
        assert client.get(result['audio_url']).status_code == 200
        assert client.get('/audio-editor/audio/../../config.json').status_code == 404
        other = client.post('/audio-editor/projects', json={'path': str(source)}).json()
        bad = client.post(f"/audio-editor/projects/{other['id']}/process", json={
            'source_version': result['id'], 'state': other['state']})
        assert bad.status_code == 400
        saved = client.patch(f'/audio-editor/projects/{identity}', json={**project['state'], 'selected_version': result['id']})
        assert saved.status_code == 200
        assert source.read_bytes() == original
    with TestClient(create_app(root)) as client:
        recovered = client.get(f'/audio-editor/projects/{identity}').json()
        assert recovered['state']['selected_version'] == result['id']
        assert len(recovered['versions']) == 2 and recovered['clip_id'] == clip_id


class FakeVevo:
    def __init__(self, root):
        self.calls = []
        self.started = threading.Event()
        self.block = False

    def generate_edit(self, voice, parameters, source, reference, output, cancelled):
        self.calls.append((voice, parameters, inspect_wave(source), inspect_wave(reference)))
        self.started.set()
        if self.block:
            cancelled.wait(5)
            raise VoiceError('CANCELLED', '编辑已取消')
        shutil.copyfile(source, output)

    def interrupt(self):
        pass


def setup_vevo(tmp_path, client):
    manager = client.app.state.manager
    model = tmp_path/'vevo.gguf'
    gguf(model, {'family': 'vevo2'})
    engine = tmp_path/'audiocpp_server.exe'
    engine.write_bytes(b'fixture')
    atomic_json(manager.config.path, {**manager.config.read(), 'native_engine': str(engine)})
    assert client.post('/audio-editor/models', json={'path': str(model)}).status_code == 201
    return str(model)


def test_vevo_processing_uses_task_specific_snapshots_and_source_selection(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.audio_editing.service.VevoRunner', FakeVevo)
    source = wav(tmp_path/'原文.wav', seconds=4)
    reference = wav(tmp_path/'角色.wav')
    with TestClient(create_app(tmp_path/'data')) as client:
        model = setup_vevo(tmp_path, client)
        project = client.post('/audio-editor/projects', json={'path': str(source)}).json()
        body = {'operation': 'vc', 'source_version': project['versions'][0]['id'], 'start': 1, 'end': 3,
                'model_path': model, 'reference_path': str(reference), 'state': project['state']}
        assert client.post(f"/audio-editor/projects/{project['id']}/process", json=body).status_code == 202
        result = wait_version(client, project['id'])
        runner = client.app.state.manager.audio_editor.runner
        assert runner.calls[0][0]['task'] == 'vc'
        assert runner.calls[0][2]['duration'] == 2
        assert result['versions'][0]['parameters']['seed'] >= 0
        body.update(operation='edit', reference_path=None, state={**project['state'], 'target_text': '新的台词'})
        assert client.post(f"/audio-editor/projects/{project['id']}/process", json=body).status_code == 202
        result = wait_version(client, project['id'])
        assert runner.calls[-1][0]['task'] == 's2s'
        assert result['versions'][0]['parameters']['state']['target_text'] == '新的台词'
        assert not client.app.state.manager.editing_busy
        assert next(m for m in client.get('/models').json()['installed'] if m['family'] == 'vevo2')['edit_adapted']


def test_cancel_and_engine_exclusion_keep_original_audio(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.audio_editing.service.VevoRunner', FakeVevo)
    source = wav(tmp_path/'original.wav')
    with TestClient(create_app(tmp_path/'data')) as client:
        model = setup_vevo(tmp_path, client)
        project = client.post('/audio-editor/projects', json={'path': str(source)}).json()
        editor = client.app.state.manager.audio_editor
        editor.runner.block = True
        body = {'operation': 'edit', 'source_version': project['versions'][0]['id'], 'model_path': model,
                'state': {**project['state'], 'target_text': '新台词'}}
        assert client.post(f"/audio-editor/projects/{project['id']}/process", json=body).status_code == 202
        assert editor.runner.started.wait(3)
        assert client.get('/health').json()['audio_editing']
        assert client.get('/health').json()['current'] is None
        assert client.post('/engine/release').status_code == 400
        assert client.put('/models/runtime', json={'path': client.app.state.manager.models.runtime()}).status_code == 400
        assert client.post(f"/audio-editor/projects/{project['id']}/cancel").status_code == 200
        final = wait_version(client, project['id'], 'cancelled')
        assert len(final['versions']) == 2 and Path(final['versions'][-1]['path']).is_file()
        assert not editor.gpu_busy
        assert not list(editor.root.glob('*-source.wav'))


def test_native_vevo_request_has_distinct_routes_without_tts_options():
    vc = native_request('vc', 'source.wav', 'voice.wav', seed=123)
    edit = native_request('edit', 'source.wav', 'voice.wav', text='new', original_text='old')
    assert vc['request']['options']['route'] == 'style_preserved_vc'
    assert edit['request']['options']['route'] == 'editing'
    assert edit['request']['options']['prosody_ref'] == 'source.wav'
    assert edit['request']['options']['style_ref_text'] == 'old'
    assert edit['request']['text'] == 'new'
    assert not {'guidance_scale', 'emotion_text', 'text_chunk_mode'} & edit['request']['options'].keys()


def test_native_adapter_stages_unicode_sources_and_receives_wav(tmp_path, monkeypatch):
    source = wav(tmp_path/'台词.wav')
    reference = wav(tmp_path/'音色.wav')
    runner = VevoRunner(tmp_path)
    monkeypatch.setattr(runner, 'start', lambda voice, cancelled: (None, 'http://native'))
    received = []
    def handler(request):
        import base64
        body = json.loads(request.content)
        options = body['request']['options']
        received.append(body)
        assert options['source_audio'].isascii()
        assert options['target_voice'].isascii()
        assert Path(options['source_audio']).read_bytes() == source.read_bytes()
        return httpx.Response(200, json={'audio': base64.b64encode(source.read_bytes()).decode()})
    original_client = httpx.Client
    monkeypatch.setattr('voice_workbench.audio_editing.vevo2.httpx.Client',
                        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    parameters = {'operation': 'edit', 'seed': 3, 'steps': 8, 'state': {'target_text': '新台词', 'original_text': '原文'}}
    output = tmp_path/'result.wav'
    runner.generate_edit({'timeout': 3}, parameters, source, reference, output, threading.Event())
    assert received[0]['request']['options']['num_inference_steps'] == 8
    assert output.read_bytes() == source.read_bytes()
    assert not Path(received[0]['request']['options']['source_audio']).exists()


def test_interrupted_edit_recovers_without_serving_partial_output(tmp_path):
    from voice_workbench.audio_editing.schemas import AudioOperation
    source = wav(tmp_path/'original.wav')
    root = tmp_path/'data'
    with TestClient(create_app(root)) as client:
        project = client.post('/audio-editor/projects', json={'path': str(source)}).json()
        editor = client.app.state.manager.audio_editor
        version = editor.store.enqueue(project['id'], AudioOperation(source_version=project['versions'][0]['id'], state=project['state']).model_dump())
        wav(editor.root/(version+'.wav'))
        wav(editor.root/(version+'-source.wav'))
        assert client.get('/audio-editor/audio/'+version+'.wav').status_code == 404
    with TestClient(create_app(root)) as client:
        recovered = client.get(f"/audio-editor/projects/{project['id']}").json()
        assert recovered['versions'][0]['status'] == 'failed'
        assert recovered['versions'][-1]['status'] == 'done'
        editor = client.app.state.manager.audio_editor
        assert not (editor.root/(version+'.wav')).exists()
        assert not (editor.root/(version+'-source.wav')).exists()
        assert client.get(recovered['versions'][-1]['audio_url']).status_code == 200


def test_ai_limits_and_wrong_family_fail_before_creating_a_version(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.audio_editing.service.VevoRunner', FakeVevo)
    source = wav(tmp_path/'long.wav', seconds=61)
    with TestClient(create_app(tmp_path/'data')) as client:
        model = setup_vevo(tmp_path, client)
        other = tmp_path/'other.gguf'
        gguf(other, {'family': 'kokoro_tts'})
        assert client.post('/audio-editor/models', json={'path': str(other)}).status_code == 400
        project = client.post('/audio-editor/projects', json={'path': str(source)}).json()
        body = {'operation': 'edit', 'source_version': project['versions'][0]['id'], 'model_path': model,
                'state': {**project['state'], 'target_text': 'short'}}
        response = client.post(f"/audio-editor/projects/{project['id']}/process", json=body)
        assert response.status_code == 400 and response.json()['error']['code'] == 'EDIT_SOURCE_TOO_LONG'
        assert len(client.get(f"/audio-editor/projects/{project['id']}").json()['versions']) == 1
        assert not client.app.state.manager.editing_busy


def test_speech_scheduler_waits_while_audio_editing_owns_engine(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.audio_editing.service.VevoRunner', FakeVevo)
    source = wav(tmp_path/'original.wav')
    with TestClient(create_app(tmp_path/'data')) as client:
        model = setup_vevo(tmp_path, client)
        project = client.post('/audio-editor/projects', json={'path': str(source)}).json()
        manager = client.app.state.manager
        manager.audio_editor.runner.block = True
        body = {'operation': 'edit', 'source_version': project['versions'][0]['id'], 'model_path': model,
                'state': {**project['state'], 'target_text': 'new'}}
        assert client.post(f"/audio-editor/projects/{project['id']}/process", json=body).status_code == 202
        assert manager.audio_editor.runner.started.wait(3)
        calls = []
        original = manager.store.next
        def next_task(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)
        with manager.lock:
            monkeypatch.setattr(manager.store, 'next', next_task)
        time.sleep(0.25)
        assert not calls
        client.post(f"/audio-editor/projects/{project['id']}/cancel")
        wait_version(client, project['id'], 'cancelled')
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls


def test_native_server_uses_different_task_sessions_for_vc_and_edit(tmp_path, monkeypatch):
    model = tmp_path/'model.gguf'
    gguf(model, {'family': 'vevo2'})
    configs, processes = [], []
    class Process:
        def __init__(self): self.returncode = None
        def poll(self): return self.returncode
        def kill(self): self.returncode = -1
        def wait(self, **kwargs): return self.returncode
    def spawn(command, **kwargs):
        configs.append(json.loads(Path(command[command.index('--config')+1]).read_text(encoding='utf-8')))
        process = Process()
        processes.append(process)
        return process
    monkeypatch.setattr('voice_workbench.audio_cpp.subprocess.Popen', spawn)
    original = httpx.Client
    monkeypatch.setattr('voice_workbench.audio_cpp.httpx.Client', lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'status': 'ok'})), **kw))
    runner = VevoRunner(tmp_path)
    voice = {'family': 'vevo2', 'model_dir': str(model), 'engine_executable': str(tmp_path/'audiocpp_server.exe'),
             'device': 'cpu', 'revision': 'fixture', 'timeout': 2, 'task': 'vc'}
    try:
        runner.start(voice, threading.Event())
        runner.start({**voice, 'task': 's2s'}, threading.Event())
        assert [c['models'][0]['task'] for c in configs] == ['vc', 's2s']
        assert processes[0].returncode == -1
        assert configs[-1]['max_loaded_models'] == 1
    finally:
        runner.interrupt()
