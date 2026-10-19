"""Domestic setup sources cover Python, wheel dependencies and paired Qwen weights."""
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import tarfile

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.batch_inference.discovery import catalog, model_path
from voice_workbench.batch_inference.schemas import SetupRequest
from voice_workbench.batch_inference.sources import ALIYUN, PYPI, TUNA, pip_plans
from voice_workbench.downloads import DownloadPaused
from test_batch_setup import manager, transfers


def test_setup_sources_default_to_domestic_and_reject_unknown_values():
    assert SetupRequest().runtime_source == 'mirror'
    assert SetupRequest().source == 'modelscope'
    with pytest.raises(ValueError):
        SetupRequest(runtime_source='https://unknown.example')


def test_setup_api_forwards_both_sources_and_rejects_unknown_runtime_source(manager, monkeypatch):
    app = create_app(manager.root)
    app.state.manager = manager
    calls = []
    monkeypatch.setattr(manager.batch.setup, 'start', lambda **kwargs: calls.append(kwargs) or {})
    client = TestClient(app)
    before = manager.config.path.read_bytes()
    assert client.post('/inference/setup', json={'runtime_source': 'unknown'}).status_code == 422
    assert not calls and manager.config.path.read_bytes() == before
    response = client.post('/inference/setup', json={
        'action': 'runtime', 'source': 'modelscope', 'runtime_source': 'official'})
    assert response.status_code == 202
    assert calls[0]['source'] == 'modelscope' and calls[0]['runtime_source'] == 'official'


def test_new_gui_rejects_service_that_cannot_use_runtime_sources(manager, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    app = create_app(manager.root)
    app.state.manager = manager
    client = TestClient(app)
    health = client.get('/health').json()
    assert health['batch_setup_sources'] is True
    monkeypatch.setattr(Window, 'call', lambda *args, **kwargs: None)
    window = Window(18992, manager.root, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.document_timer, window.settings.timer):
        timer.stop()
    errors, polls = [], []
    monkeypatch.setattr(window, 'show_error', errors.append)
    monkeypatch.setattr(window, 'poll', lambda: polls.append(True))
    old = dict(health)
    old.pop('batch_setup_sources')
    window._existing_service(old)
    assert not window.ready and errors and '旧版本' in errors[0] and not polls
    window._existing_service(health)
    assert window.ready and polls == [True]
    window.close()


@pytest.mark.parametrize('device,variant', [('cuda', 'cu128'), ('cpu', 'cpu')])
def test_wheel_plans_keep_exact_compute_build_and_use_working_index_formats(device, variant):
    plans = pip_plans(['python', '-m', 'pip', 'install'], [], 'mirror', device)
    assert len(plans) == 3
    for _, command in plans:
        assert f'torch==2.9.0+{variant}' in command
        assert f'torchaudio==2.9.0+{variant}' in command
    assert f'https://mirror.sjtu.edu.cn/pytorch-wheels/{variant}/' in plans[0][1]
    assert TUNA in plans[0][1]
    assert plans[1][1][-2:] == ['--find-links', f'https://mirrors.aliyun.com/pytorch-wheels/{variant}/']
    assert ALIYUN in plans[1][1]
    official = pip_plans(['pip'], [], 'official', device)
    assert len(official) == 1 and PYPI in official[0][1]
    assert f'https://download.pytorch.org/whl/{variant}' in official[0][1]


def test_domestic_pip_fallback_and_pause_are_bounded(manager, monkeypatch):
    task = manager.batch.setup
    calls = []

    def command(args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError('mirror unavailable')

    monkeypatch.setattr(task, '_command', command)
    task._pip(['pip', 'install'], ['qwen-tts==0.1.1'])
    assert len(calls) == 2 and TUNA in calls[0] and ALIYUN in calls[1]
    assert task.status()['download_source'] == '阿里云 PyPI 镜像'
    assert len(task.status()['source_errors']) == 1
    calls.clear()

    def paused(args):
        calls.append(args)
        task.event.set()
        raise RuntimeError('interrupted')

    monkeypatch.setattr(task, '_command', paused)
    with pytest.raises(DownloadPaused):
        task._pip(['pip', 'install'], ['pip==25.2'])
    assert len(calls) == 1


def test_failed_dependency_sources_preserve_configuration_and_report_each_attempt(manager, monkeypatch):
    task = manager.batch.setup
    before = manager.config.path.read_bytes()
    calls = []

    def failed(command):
        calls.append(command)
        raise RuntimeError('connection timeout')

    monkeypatch.setattr(task, '_command', failed)
    with pytest.raises(RuntimeError, match='依赖安装失败') as error:
        task._pip(['pip', 'install'], ['qwen-tts==0.1.1'])
    assert len(calls) == 3 and '清华' in str(error.value) and '阿里云' in str(error.value) and '官方' in str(error.value)
    assert manager.config.path.read_bytes() == before


def test_python_archive_uses_domestic_transport_and_reuses_existing_partial(manager, monkeypatch):
    task = manager.batch.setup
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as package:
        member = tarfile.TarInfo('python/python.exe')
        member.size = len(b'portable Python')
        package.addfile(member, io.BytesIO(b'portable Python'))
    payload = stream.getvalue()
    data = deepcopy(catalog())
    data['python'].update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr('voice_workbench.batch_inference.provisioning.catalog', lambda: data)
    target = manager.root/'runtime/qwen-batch/downloads'/data['python']['name']
    target.parent.mkdir(parents=True)
    part = target.with_name(target.name + '.part')
    part.write_bytes(payload[:20])
    requests = []

    def handle(request):
        requests.append(request)
        assert task.status()['download_source'] == 'ghfast 加速'
        assert request.headers['range'] == 'bytes=20-'
        return httpx.Response(206, content=payload[20:], headers={'Content-Range': f'bytes 20-{len(payload)-1}/{len(payload)}'})

    task.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handle))
    commands = []

    def command(args, timeout=3600):
        commands.append(args)
        if 'venv' in args:
            python = Path(args[-1])/'Scripts/python.exe'
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b'fixture')

    monkeypatch.setattr(task, '_command', command)
    monkeypatch.setattr(manager.batch, 'diagnose_python', lambda _: ('', {}))
    assert Path(task._runtime('cuda')).is_file()
    assert len(requests) == 1 and requests[0].url.host == 'ghfast.top'
    assert not part.exists()
    assert any('torch==2.9.0+cu128' in c and TUNA in c for c in commands)
    assert any('qwen-tts==0.1.1' in c and TUNA in c for c in commands)


def test_modelscope_resumes_the_same_verified_model_files(manager, transfers):
    task = manager.batch.setup
    root = model_path(manager.root, '1.7B')
    root.mkdir(parents=True)
    (root/'model.safetensors.part').write_bytes(transfers[0]['model.safetensors'][:262144])
    task._model('1.7B', 'modelscope')
    assert task.status()['download_source'] == 'ModelScope 魔搭（国内）'
    assert all(r.url.host == 'modelscope.cn' for r in transfers[1])
    assert any(r.headers.get('range') == 'bytes=262144-' for r in transfers[1])
    assert (root/'model.safetensors').read_bytes() == transfers[0]['model.safetensors']


def test_changed_modelscope_bytes_are_rejected_using_catalog_hash(manager, transfers):
    task = manager.batch.setup
    def wrong(request):
        raw = transfers[0]['config.json']
        return httpx.Response(200, content=raw[:-1] + b'?')
    task.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(wrong))
    with pytest.raises(ValueError, match='SHA-256'):
        task._model('1.7B', 'modelscope')
    root = model_path(manager.root, '1.7B')
    assert not (root/'config.json').exists() and not (root/'config.json.part').exists()


def test_setup_ui_submits_both_source_choices_and_keeps_them_during_polling(manager, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    monkeypatch.setattr(Window, 'call', lambda *args, **kwargs: None)
    window = Window(18992, manager.root, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.settings.timer, window.document_timer):
        timer.stop()
    panel = window.settings.batch_inference
    control = panel.automatic
    data = manager.batch.setup.options()
    control.bind(data, manager.batch.settings.model_dump())
    assert control.runtime_source.currentData() == 'mirror'
    assert control.download_source.currentData() == 'modelscope'
    control.runtime_source.setCurrentIndex(control.runtime_source.findData('official'))
    control.download_source.setCurrentIndex(control.download_source.findData('mirror'))
    control.bind(data, manager.batch.settings.model_dump())
    posts = []
    class Client:
        def post(self, url, **kwargs):
            posts.append((url, kwargs['json']))
            return httpx.Response(202, json={})
    monkeypatch.setattr(panel, 'refresh', lambda: None)
    def call(action, success=None, failure=None):
        value = action(Client())
        if success:
            success(value)
    monkeypatch.setattr(window, 'call', call)
    control.start('all')
    assert posts[0][1]['runtime_source'] == 'official' and posts[0][1]['source'] == 'mirror'
    data['task'].update(busy=True, status='dependencies')
    control.bind(data, manager.batch.settings.model_dump())
    assert not control.runtime_source.isEnabled() and not control.download_source.isEnabled()
    window.close()
