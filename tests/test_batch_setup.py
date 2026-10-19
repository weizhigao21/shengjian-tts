"""Managed setup lifecycle, verified transfers and automatic local association."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.batch_inference.discovery import base_size, catalog, complete_model, discover, model_path, source_size
from voice_workbench.batch_inference.provisioning import BatchSetup
from voice_workbench.batch_inference.schemas import BatchSettings
from voice_workbench.config import ConfigStore
from voice_workbench.downloads import DownloadPaused
from voice_workbench.errors import VoiceError
from voice_workbench.manager import Manager
from test_batch_inference import assets
from test_model_capabilities import gguf
from test_service import ControlledRunner, wait_job


def config(size='1.7B', variant='base'):
    return {'model_type': 'qwen3_tts', 'tts_model_type': variant,
            'talker_config': {'hidden_size': 2048 if size == '1.7B' else 1024,
                              'num_hidden_layers': 28, 'num_attention_heads': 16}}


def stock_voice(tmp_path, size='1.7B'):
    voice, _ = assets(tmp_path)
    gguf(Path(voice['model_dir']), {'schema_version': 1, 'family': 'qwen3_tts', 'languages': ['zh', 'en'],
         'capabilities': {'clone': ['speaker_reference']}}, config=config(size))
    return voice


def local_model(path, size='1.7B'):
    (path/'speech_tokenizer').mkdir(parents=True)
    (path/'config.json').write_text(json.dumps(config(size)))
    (path/'model.safetensors').write_bytes(b'weights')
    (path/'speech_tokenizer/model.safetensors').write_bytes(b'tokenizer')
    (path/'shengjian-source.json').write_text(json.dumps({'repo': 'fixture', 'revision': 'fixture'}))
    return str(path)


def wait_setup(setup):
    setup.thread.join(timeout=8)
    assert not setup.busy
    return setup.status()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    voice = stock_voice(tmp_path)
    ConfigStore(tmp_path/'data').save_voice(voice)
    m = Manager(tmp_path/'data', ControlledRunner())
    if m.batch.setup.thread:
        wait_setup(m.batch.setup)
    yield m
    m.close()


def test_auto_detect_matches_embedded_metadata_and_never_uses_filename_guess(manager, tmp_path):
    settings, sources, selected = discover(manager.batch)
    assert selected['size'] == '1.7B' and settings.source_model.endswith('qwen.gguf')
    misleading = tmp_path/'Qwen3-TTS-0.6B-Base.gguf'
    misleading.write_bytes(b'not a gguf')
    assert source_size(misleading) is None
    misleading.write_bytes(b'GGUF')
    assert source_size(misleading) is None
    assert base_size(config(variant='custom_voice')) is None
    assert base_size({**config(), 'talker_config': {'hidden_size': 2048}}) is None
    assert discover(manager.batch, str(misleading))[2] is None


def test_discovery_reuses_managed_models_and_environment_without_network(manager, monkeypatch):
    backend = manager.batch
    path = model_path(manager.root, '1.7B')
    local_model(path)
    python = manager.root/'runtime/qwen-batch/env-cuda/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'external-interpreter')
    candidate, _, _ = discover(backend)
    assert candidate.python == str(python) and candidate.model_dir == str(path)
    local_model(manager.root/'unrelated', '0.6B')
    assert not complete_model(manager.root/'unrelated', '1.7B')
    assert not backend.process.process
    backend.settings = candidate
    monkeypatch.setattr(manager.models, 'installed', lambda: [])
    assert discover(backend, device='cuda')[0].python == str(python)
    assert not discover(backend, device='cpu')[0].python


def test_local_huggingface_cache_is_detected_but_wrong_variant_is_ignored(manager, tmp_path, monkeypatch):
    hub = tmp_path/'hub'
    monkeypatch.setenv('HF_HUB_CACHE', str(hub))
    path = hub/'models--Qwen--Qwen3-TTS-12Hz-1.7B-Base/snapshots/revision'
    local_model(path)
    assert discover(manager.batch)[0].model_dir == str(path)
    (path/'config.json').write_text(json.dumps(config(variant='custom_voice')))
    assert not discover(manager.batch)[0].model_dir


def test_detect_automatically_applies_reused_files_and_preserves_other_settings(manager, monkeypatch):
    before = manager.config.read()
    local_model(model_path(manager.root, '1.7B'))
    python = manager.root/'python.exe'; python.write_bytes(b'fixture')
    manager.batch.settings = BatchSettings(python=str(python), max_batch_size=4, enabled=False)
    manager.batch.reason = '自动批量推理已关闭。'
    task = manager.batch.setup
    task.last_scan = 0
    task.ensure_scan()
    assert wait_setup(task)['status'] == 'done'
    assert manager.batch.settings.source_model and manager.batch.settings.model_dir
    assert manager.batch.settings.max_batch_size == 4 and not manager.batch.settings.enabled
    assert manager.config.read()['voices'] == before['voices']
    assert manager.batch.process.process is None


@pytest.fixture
def transfers(manager, monkeypatch):
    payloads = {'config.json': json.dumps(config()).encode(), 'model.safetensors': b'x'*800000,
                'speech_tokenizer/model.safetensors': b'tokenizer'}
    data = deepcopy(catalog())
    data['models']['1.7B'].update(revision='a'*40, files=[{'path': name, 'size': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in payloads.items()])
    monkeypatch.setattr('voice_workbench.batch_inference.discovery.catalog', lambda: data)
    monkeypatch.setattr('voice_workbench.batch_inference.provisioning.catalog', lambda: data)
    requests = []
    def reply(request):
        requests.append(request)
        name = request.url.path.split('/resolve/', 1)[1].split('/', 1)[1]
        raw = payloads[name]
        offset = int(request.headers.get('range', 'bytes=0-').split('=')[1].split('-')[0])
        headers = {'content-length': str(len(raw)-offset)}
        if offset: headers['content-range'] = f'bytes {offset}-{len(raw)-1}/{len(raw)}'
        return httpx.Response(206 if offset else 200, headers=headers, content=raw[offset:])
    manager.batch.setup.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(reply))
    return payloads, requests


def test_verified_paired_model_download_reuses_files_and_pins_revision(manager, transfers):
    task = manager.batch.setup
    path = Path(task._model('1.7B', 'mirror'))
    assert complete_model(path, '1.7B') and len(transfers[1]) == 3
    task._model('1.7B', 'huggingface')
    assert len(transfers[1]) == 3
    assert all('/resolve/'+'a'*40+'/' in str(r.url) for r in transfers[1])
    assert json.loads((path/'shengjian-source.json').read_text())['revision'] == 'a'*40
    assert task.status()['speed_bps'] == 0


def test_model_download_resumes_partial_weight_file(manager, transfers):
    task = manager.batch.setup
    path = model_path(manager.root, '1.7B')
    path.mkdir(parents=True)
    part = path/'model.safetensors.part'
    part.write_bytes(transfers[0]['model.safetensors'][:262144])
    task._model('1.7B', 'huggingface')
    assert any(r.headers.get('range') == 'bytes=262144-' for r in transfers[1])
    assert not part.exists()
    assert (path/'model.safetensors').read_bytes() == transfers[0]['model.safetensors']


def test_corrupt_managed_weight_can_be_retried_without_touching_external_files(manager, transfers, tmp_path):
    task = manager.batch.setup
    path = model_path(manager.root, '1.7B')
    path.mkdir(parents=True)
    corrupt = path/'model.safetensors'; corrupt.write_bytes(b'broken')
    external = tmp_path/'keep.safetensors'; external.write_bytes(b'keep')
    with pytest.raises(ValueError, match='大小'):
        task._model('1.7B', 'mirror')
    assert not corrupt.exists() and external.read_bytes() == b'keep'
    task._model('1.7B', 'mirror')
    assert complete_model(path, '1.7B')


def test_one_click_installs_then_applies_and_rejects_concurrent_install(manager, transfers, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    python = manager.root/'python.exe'; python.write_bytes(b'fixture')
    def runtime(device):
        entered.set(); release.wait(3)
        return str(python)
    monkeypatch.setattr(manager.batch.setup, '_runtime', runtime)
    monkeypatch.setattr(manager.batch, 'diagnose', lambda settings=None: ('', {'qwen_tts': 'test'}))
    manager.batch.setup.start()
    assert entered.wait(2)
    assert not manager.batch.status()['ready'] and manager.batch.plan({}, ['a', 'b'])['requires_setup']
    with pytest.raises(VoiceError, match='暂停'):
        manager.batch.setup.start()
    with pytest.raises(VoiceError, match='暂停'):
        manager.batch.configure(BatchSettings())
    release.set()
    assert wait_setup(manager.batch.setup)['status'] == 'done'
    assert manager.batch.settings.python == str(python) and not manager.batch.reason
    assert manager.batch.settings.model_dir and manager.batch.settings.source_model
    assert not manager.batch.process.process


def test_pause_keeps_previous_config_and_can_continue(manager, transfers, monkeypatch):
    entered = threading.Event()
    def runtime(device):
        entered.set()
        manager.batch.setup.event.wait(3)
        manager.batch.setup.check_cancel()
    monkeypatch.setattr(manager.batch.setup, '_runtime', runtime)
    before = manager.config.path.read_bytes()
    manager.batch.setup.start()
    assert entered.wait(2)
    manager.batch.setup.pause()
    assert wait_setup(manager.batch.setup)['status'] == 'paused'
    assert manager.config.path.read_bytes() == before
    manager.batch.setup.last_scan = 0
    manager.batch.setup.ensure_scan()
    assert manager.batch.setup.status()['status'] == 'paused'
    manager.batch.setup.start('model')
    assert wait_setup(manager.batch.setup)['status'] == 'done'
    assert manager.batch.settings.model_dir


def test_failed_runtime_does_not_commit_partial_settings_and_is_retryable(manager, monkeypatch):
    def fail(device): raise RuntimeError('network unavailable')
    monkeypatch.setattr(manager.batch.setup, '_runtime', fail)
    before = manager.config.path.read_bytes()
    manager.batch.setup.start('runtime')
    assert wait_setup(manager.batch.setup)['status'] == 'failed'
    assert manager.config.path.read_bytes() == before
    assert 'network unavailable' in manager.batch.setup.status()['message']
    manager.batch.setup.start('detect')
    assert wait_setup(manager.batch.setup)['status'] == 'done'


def test_setup_waits_for_running_tasks_before_applying(manager, monkeypatch):
    task = manager.batch.setup
    previous = manager.batch.settings.model_dump()
    candidate = manager.batch.settings.model_copy(update={'max_batch_size': 4})
    manager.active['fixture'] = {}
    t = threading.Thread(target=task._apply, args=(candidate, previous))
    t.start()
    deadline = time.monotonic()+2
    while task.status()['status'] != 'waiting' and time.monotonic() < deadline: time.sleep(.01)
    assert manager.batch.settings.max_batch_size == 2
    manager.active.clear()
    t.join(timeout=2)
    assert not t.is_alive() and manager.batch.settings.max_batch_size == 4


@pytest.mark.parametrize('name,kind', [('python/../../escape', 'file'), ('/escape', 'file'),
                                    ('python/link', 'symlink'), ('C:/escape', 'file')])
def test_runtime_extraction_rejects_traversal_and_links(manager, name, kind):
    archive = manager.root/'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as package:
        member = tarfile.TarInfo(name)
        if kind == 'symlink': member.type = tarfile.SYMTYPE; member.linkname = '../outside'
        else: member.size = 3
        package.addfile(member, io.BytesIO(b'bad'))
    with pytest.raises(ValueError, match='路径'):
        manager.batch.setup._extract(archive, manager.root/'extract')
    assert not (manager.root.parent/'escape').exists()


def test_owned_command_is_cancellable_and_does_not_change_parent_environment(manager):
    task = manager.batch.setup
    original = dict(os.environ)
    errors = []
    def run():
        try: task._command([sys.executable, '-c', 'import time; time.sleep(30)'])
        except DownloadPaused: errors.append('paused')
    t = threading.Thread(target=run); t.start()
    time.sleep(.2); task.event.set(); t.join(timeout=5)
    assert not t.is_alive() and errors == ['paused']
    assert dict(os.environ) == original


def test_runtime_install_is_isolated_and_reuses_a_completed_environment(manager, monkeypatch):
    task = manager.batch.setup
    task.runtime_source = 'official'
    root = manager.root/'runtime/qwen-batch'
    root.mkdir(parents=True)
    (root/'python').mkdir()
    (root/'python/python.exe').write_bytes(b'fixture')
    commands = []
    def command(args, timeout=3600):
        commands.append(args)
        if 'venv' in args:
            python = Path(args[-1])/'Scripts/python.exe'
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b'fixture')
    monkeypatch.setattr(task, '_command', command)
    monkeypatch.setattr(manager.batch, 'diagnose_python', lambda _: ('', {}))
    python = task._runtime('cpu')
    assert Path(python).is_relative_to(manager.root)
    assert any('qwen-tts==0.1.1' in command for command in commands)
    assert any('https://download.pytorch.org/whl/cpu' in command for command in commands)
    assert (root/'env-cpu/shengjian-runtime.json').is_file()
    commands.clear()
    assert task._runtime('cpu') == python
    assert len(commands) == 1 and commands[0][1] == '-c'


def test_automatic_refresh_checks_changed_files_once_without_repeated_dependency_probes(manager, monkeypatch):
    model = model_path(manager.root, '1.7B'); local_model(model)
    python = manager.root/'runtime/env/Scripts/python.exe'
    python.parent.mkdir(parents=True); python.write_bytes(b'fixture')
    manager.batch.settings = BatchSettings(python=str(python), model_dir=str(model),
                                          source_model=manager.config.voices()[0]['model_dir'])
    task = manager.batch.setup
    calls = []
    monkeypatch.setattr(task, 'start', lambda action: calls.append(action))
    task.last_scan = 0; task.ensure_scan()
    task.last_scan = 0; task.ensure_scan()
    assert calls == ['detect']
    package = python.parent.parent/'Lib/site-packages/qwen_tts/__init__.py'
    package.parent.mkdir(parents=True); package.write_text('# installed')
    task.last_scan = 0; task.ensure_scan()
    assert calls == ['detect', 'detect']


def test_setup_api_is_async_and_polling_does_not_start_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    voice = stock_voice(tmp_path)
    ConfigStore(tmp_path/'data').save_voice(voice)
    with TestClient(create_app(tmp_path/'data', ControlledRunner())) as c:
        m = c.app.state.manager
        if m.batch.setup.thread:
            wait_setup(m.batch.setup)
        def forbidden(*_): raise AssertionError('polls cannot download')
        monkeypatch.setattr(m.batch.setup, '_runtime', forbidden)
        for _ in range(2):
            data = c.get('/settings/batch-inference').json()
            assert data['setup']['sources'][0]['size'] == '1.7B'
            assert data['setup']['task']['status'] in ('idle', 'done')
        assert c.post('/inference/setup', json={'action': 'bad'}).status_code == 422
        assert c.post('/inference/setup', json={'source_model': 'unregistered.gguf'}).status_code == 400
        r = c.post('/inference/setup', json={'action': 'detect'})
        assert r.status_code == 202
        assert wait_setup(m.batch.setup)['status'] == 'done'
        assert c.post('/inference/setup/pause').status_code == 200


def test_setup_ui_retains_choice_shows_speed_and_refreshes_on_completion(manager, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    monkeypatch.setattr(Window, 'call', lambda *a, **kw: None)
    window = Window(18992, manager.root, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.settings.timer, window.document_timer): timer.stop()
    control = window.settings.batch_inference
    setup = manager.batch.setup.options()
    control.bind({**manager.batch.status(), 'setup': setup})
    auto = control.automatic
    assert auto.source.currentData() == setup['sources'][0]['path']
    assert not control.manual.isVisible() and auto.install.isEnabled()
    setup['task'].update(busy=True, status='downloading', received=1048576, size=2097152,
                         speed_bps=2097152, message='model.safetensors')
    auto.bind(setup, control.settings)
    assert auto.timer.isActive() and auto.progress.value() == 500
    assert '2.00 MB/s' in auto.status.text() and not auto.install.isEnabled()
    setup['task'].update(busy=False, status='done', message='自动配置完成', speed_bps=0)
    auto.bind(setup, control.settings)
    assert not auto.timer.isActive() and auto.install.isEnabled()
    assert '完成' in auto.status.text()
    window.close()


def test_batch_ui_binds_and_saves_start_threshold_with_valid_bounds(manager, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    monkeypatch.setattr(Window, 'call', lambda *a, **kw: None)
    window = Window(18992, manager.root, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.settings.timer, window.document_timer): timer.stop()
    control = window.settings.batch_inference
    data = manager.batch.status()
    data['settings'].update(max_batch_size=8, min_batch_size=8)
    control.bind(data)
    assert control.size.value() == control.minimum_size.value() == 8
    assert not control.dirty
    control.size.setValue(4)
    assert control.minimum_size.maximum() == control.minimum_size.value() == 4
    captured = {}
    def put(path, json):
        import httpx
        captured.update(json)
        return httpx.Response(200, json={}, request=httpx.Request('PUT', 'http://test'+path))
    from types import SimpleNamespace
    window.call = lambda action, *args: action(SimpleNamespace(put=put))
    control.save()
    assert captured['min_batch_size'] == captured['max_batch_size'] == 4
    window.close()
