"""Cache-free timings, exclusive inference reservation, failures and cancellation."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.batch_inference.benchmark import InferenceBenchmark, workload
from voice_workbench.config import ConfigStore, atomic_json
from voice_workbench.errors import VoiceError
from test_batch_inference import assets, batch_manager, BatchProbe
from test_service import ControlledRunner, make_wave, wait_job


class NativeProbe:
    def __init__(self):
        self.calls = []
        self.references = []
        self.interrupts = 0
        self.block = threading.Event()
        self.fail = False

    def generate(self, request, cancelled):
        self.calls.append(deepcopy(request))
        self.references.append(Path(request['voice']['reference_audio']).read_bytes())
        while self.block.is_set() and not cancelled.is_set(): time.sleep(.01)
        if cancelled.is_set(): raise VoiceError('CANCELLED', 'cancelled')
        if self.fail: raise RuntimeError('native failed')
        time.sleep(.002)
        make_wave(Path(request['output']))

    def interrupt(self):
        self.interrupts += 1


@pytest.fixture
def benchmark(batch_manager):
    manager, queue, _ = batch_manager
    native, batch = NativeProbe(), BatchProbe()
    manager.benchmark.native_factory = lambda root, threads: native
    manager.benchmark.batch_factory = lambda root: batch
    yield manager, native, batch, queue
    native.block.clear(); batch.block.clear()
    manager.benchmark.cancel()
    if manager.benchmark.thread: manager.benchmark.thread.join(timeout=5)


def finished(manager):
    manager.benchmark.thread.join(timeout=5)
    assert not manager.benchmark.gpu_busy
    assert not manager.benchmark.thread.is_alive()
    return manager.benchmark.status()


def test_identical_workload_and_seed_are_measured_without_cache_or_user_task_changes(benchmark):
    manager, native, batch, queue = benchmark
    before = manager.config.path.read_bytes()
    voice, _ = manager.config.resolve('qwen')
    reference = Path(voice['reference_audio']).read_bytes()
    manager.benchmark.start('qwen')
    state = finished(manager)
    assert state['status'] == 'done' and state['completed'] == state['total'] == 16
    report = state['result']
    assert not report['cache_used'] and len(native.calls) == 8
    assert [len(call['items']) for call in batch.calls] == [2, 2, 2, 2]
    assert [r['text'] for r in native.calls[:4]] == [r['text'] for call in batch.calls[:2] for r in call['items']]
    assert all(r['voice']['request_options']['seed'] == 1234 for r in native.calls)
    assert all(call['seed'] == 1234 for call in batch.calls)
    assert all(raw == reference for raw in native.references)
    assert [i['language'] for call in batch.calls[:2] for i in call['items']] == ['Chinese', 'English', 'Chinese', 'English']
    a, b = report['results']
    assert a['cold']['calls'] == 4 and b['cold']['calls'] == 2
    assert a['warm']['audio_seconds'] == b['warm']['audio_seconds'] == 4
    assert a['warm']['rtf'] == a['warm']['seconds']/4
    assert report['speedup']['warm'] == a['warm']['seconds']/b['warm']['seconds']
    assert not queue.calls and not manager.store.list() and not manager.generated_audio()
    assert manager.config.path.read_bytes() == before
    assert Path(voice['reference_audio']).read_bytes() == reference
    assert not list((manager.root/'temporary').glob('benchmark-*'))
    assert native.interrupts and not batch.process
    saved = json.loads(manager.benchmark.path.read_text(encoding='utf-8'))
    assert saved['status'] == 'done'
    assert InferenceBenchmark(manager).status()['result']['id'] == report['id']


def test_warm_round_average_and_configured_batch_limit_are_respected(benchmark):
    manager, native, batch, _ = benchmark
    manager.batch.settings = manager.batch.settings.model_copy(update={'max_batch_size': 3})
    manager.benchmark.start('qwen', 'en', 3)
    result = finished(manager)['result']
    assert len(native.calls) == 16 and [len(c['items']) for c in batch.calls] == [3, 1]*4
    for backend in result['results']:
        rounds = backend['warm_rounds']
        assert len(rounds) == 3
        assert backend['warm']['seconds'] == sum(r['seconds'] for r in rounds)/3
    assert all(item['language'] == 'English' for call in batch.calls for item in call['items'])


@pytest.mark.parametrize('backend', ['native', 'batch'])
def test_backend_failure_keeps_other_result_and_never_reports_fallback_as_batch_speed(benchmark, backend):
    manager, native, batch, _ = benchmark
    (native if backend == 'native' else batch).fail = True
    manager.benchmark.start('qwen')
    state = finished(manager)
    assert state['status'] == 'failed' and 'speedup' not in state['result']
    results = {r['backend']: r for r in state['result']['results']}
    key = 'native' if backend == 'native' else 'qwen3_batch'
    assert results[key]['status'] == 'failed' and results[key]['error']['message']
    assert len(native.calls) == (1 if backend == 'native' else 8)
    assert len(batch.calls) == (4 if backend == 'native' else 1)


@pytest.mark.parametrize('backend', ['native', 'batch'])
def test_cancellation_releases_reservation_and_keeps_partial_report(benchmark, backend):
    manager, native, batch, _ = benchmark
    target = native if backend == 'native' else batch
    target.block.set()
    manager.benchmark.start('qwen')
    deadline = time.monotonic()+3
    while not target.calls and time.monotonic() < deadline: time.sleep(.01)
    assert target.calls and manager.editing_busy
    manager.benchmark.cancel()
    state = finished(manager)
    assert state['status'] == 'cancelled' and not manager.editing_busy
    assert 'speedup' not in state['result']
    assert all(r['status'] != 'running' for r in state['result']['results'])
    assert not list((manager.root/'temporary').glob('benchmark-*'))


def test_running_benchmark_blocks_engine_mutations_and_queues_normal_generation(benchmark):
    manager, native, _, queue = benchmark
    native.block.set()
    manager.benchmark.start('qwen')
    deadline = time.monotonic()+2
    while not native.calls and time.monotonic() < deadline: time.sleep(.01)
    for action in (lambda: manager.set_performance({'workflow_workers': 1, 'cpu_threads': 2}),
                   lambda: manager.batch.configure(manager.batch.settings),
                   lambda: manager.batch.setup.start('runtime'),
                   lambda: manager.benchmark.start('qwen')):
        with pytest.raises(VoiceError): action()
    identity = manager.submit('queued during benchmark', 'qwen', [{'text': 'after'}])
    time.sleep(.05)
    assert not queue.calls and manager.store.get(identity)['status'] == 'queued'
    manager.benchmark.cancel()
    finished(manager)
    assert wait_job(manager.store.get, identity)['status'] == 'done'
    assert queue.calls == ['after']


def test_unready_or_incompatible_backend_is_rejected_before_releasing_models(benchmark):
    manager, native, _, _ = benchmark
    manager.batch.reason = 'missing environment'
    with pytest.raises(VoiceError, match='missing environment'):
        manager.benchmark.start('qwen')
    assert not native.calls and not manager.benchmark.gpu_busy
    manager.batch.reason = ''
    manager.batch.settings = manager.batch.settings.model_copy(update={'device': 'cpu'})
    with pytest.raises(VoiceError, match='设备'):
        manager.benchmark.start('qwen')
    assert not manager.benchmark.gpu_busy


def test_voice_snapshot_and_config_do_not_follow_mid_test_preset_edits(benchmark):
    manager, native, batch, _ = benchmark
    native.block.set()
    manager.benchmark.start('qwen')
    deadline = time.monotonic()+2
    while not native.calls and time.monotonic() < deadline: time.sleep(.01)
    voice, _ = manager.config.resolve('qwen')
    manager.config.save_voice({**voice, 'request_options': {'seed': 99, 'max_tokens': 128}})
    native.block.clear()
    assert finished(manager)['status'] == 'done'
    assert all(call['voice']['request_options']['seed'] == 1234 for call in native.calls)
    assert all(call['seed'] == 1234 for call in batch.calls)
    assert manager.config.resolve('qwen')[0]['request_options']['seed'] == 99


def test_api_exposes_async_run_progress_report_and_validates_limits(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    monkeypatch.setattr('voice_workbench.batch_inference.backend.BatchBackend.diagnose', lambda *a: ('', {}))
    voice, settings = assets(tmp_path)
    config = ConfigStore(tmp_path/'data'); config.save_voice(voice)
    atomic_json(config.path, {**config.read(), 'batch_inference': settings.model_dump()})
    with TestClient(create_app(tmp_path/'data', ControlledRunner())) as c:
        manager = c.app.state.manager
        if manager.batch.setup.thread: manager.batch.setup.thread.join(timeout=2)
        manager.benchmark.native_factory = lambda *a: NativeProbe()
        manager.benchmark.batch_factory = lambda *a: BatchProbe()
        data = c.get('/inference/benchmark').json()
        assert data['voices'][0]['id'] == 'qwen'
        assert 'benchmark' in c.get('/settings/batch-inference').json()
        assert c.post('/inference/benchmark', json={'voice_id': 'qwen', 'warm_rounds': 0}).status_code == 422
        assert c.post('/inference/benchmark', json={'voice_id': 'qwen', 'workload_name': 'x'}).status_code == 422
        assert c.post('/inference/benchmark', json={'voice_id': 'qwen'}).status_code == 202
        assert finished(manager)['status'] == 'done'
        assert c.get('/inference/benchmark').json()['result']['cache_used'] is False
        assert c.post('/inference/benchmark/cancel').status_code == 200
        assert not manager.store.list()


def test_ui_updates_results_speedup_progress_and_retains_voice_choice(benchmark, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    manager, _, _, _ = benchmark
    monkeypatch.setattr(Window, 'call', lambda *a, **kw: None)
    window = Window(18992, manager.root, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.settings.timer, window.document_timer): timer.stop()
    panel = window.settings.batch_inference
    panel.bind({**manager.batch.status(), 'setup': manager.batch.setup.options(), 'benchmark': manager.benchmark.options()})
    controls = panel.benchmark
    assert controls.start_button.isEnabled() and controls.voice.currentData() == 'qwen'
    manager.benchmark.start('qwen')
    finished(manager)
    controls.bind(manager.benchmark.options())
    assert controls.table.rowCount() == 2 and '倍率' in controls.summary.text()
    controls.copy()
    from PyQt6.QtWidgets import QApplication
    assert json.loads(QApplication.clipboard().text())['cache_used'] is False
    assert not controls.timer.isActive()
    data = manager.benchmark.options()
    data.update(busy=True, status='running', completed=4, total=16)
    controls.bind(data)
    assert controls.timer.isActive() and controls.progress.value() == 4
    assert not controls.start_button.isEnabled() and not panel.automatic.isEnabled()
    window.close()
