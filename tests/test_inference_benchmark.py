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
from voice_workbench.batch_inference.policy import compatible, unsupported
from voice_workbench.config import ConfigStore, atomic_json
from voice_workbench.errors import VoiceError
from voice_workbench.generation_options import FALLBACK
from test_batch_inference import assets, batch_manager, BatchProbe
from test_model_capabilities import gguf
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
    assert state['status'] == 'done' and state['completed'] == state['total'] == 80
    report = state['result']
    assert report['sample_count'] == 20
    assert not report['cache_used'] and len(native.calls) == 40
    assert [len(call['items']) for call in batch.calls] == [2]*20
    assert [r['text'] for r in native.calls[:20]] == [r['text'] for call in batch.calls[:10] for r in call['items']]
    assert all(r['voice']['request_options']['seed'] == 1234 for r in native.calls)
    assert all(call['seed'] == 1234 for call in batch.calls)
    assert all(raw == reference for raw in native.references)
    assert [i['language'] for call in batch.calls[:10] for i in call['items']] == ['Chinese', 'English']*10
    a, b = report['results']
    assert a['cold']['calls'] == 20 and b['cold']['calls'] == 10
    assert a['warm']['audio_seconds'] == b['warm']['audio_seconds'] == 20
    assert a['warm']['rtf'] == a['warm']['seconds']/20
    for backend in (a, b):
        for phase in ('cold', 'warm'):
            assert backend[phase]['seconds_per_item'] == backend[phase]['seconds']/20
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
    assert len(native.calls) == 80 and [len(c['items']) for c in batch.calls] == ([3]*6+[2])*4
    for backend in result['results']:
        rounds = backend['warm_rounds']
        assert len(rounds) == 3
        assert backend['warm']['seconds'] == sum(r['seconds'] for r in rounds)/3
        assert backend['warm']['seconds_per_item'] == sum(r['seconds'] for r in rounds)/60
    assert all(item['language'] == 'English' for call in batch.calls for item in call['items'])


def test_report_records_actual_predictor_path_and_option(benchmark, monkeypatch):
    manager, _, batch, _ = benchmark
    original = batch.generate
    monkeypatch.setattr(batch, 'generate', lambda *a, **kw: dict(original(*a, **kw), predictor_mode='fixed'))
    manager.benchmark.start('qwen')
    report = finished(manager)['result']
    assert report['predictor_fastpath'] is True
    result = report['results'][1]
    assert result['cold']['predictor_modes'] == ['fixed']
    assert result['warm_rounds'][0]['predictor_modes'] == ['fixed']


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
    assert len(native.calls) == (1 if backend == 'native' else 40)
    assert len(batch.calls) == (20 if backend == 'native' else 1)


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
    assert '报告：中英双语' in controls.summary.text() and '每批最多 2 条' in controls.summary.text()
    assert controls.table.columnCount() == 7
    for index, result in enumerate(controls.result['results']):
        assert controls.table.item(index, 3).text() == f"{result['warm']['seconds_per_item']:.2f}"
    old = manager.benchmark.options()
    old['result'].pop('sample_count')
    old['result']['workload'] = old['result']['workload'][:4]
    for result in old['result']['results']:
        result['warm'].pop('seconds_per_item')
        result['warm']['seconds'] = 8
    controls.bind(old)
    assert '每组 4 条' in controls.summary.text()
    assert controls.table.item(0, 3).text() == '2.00'
    controls.copy()
    from PyQt6.QtWidgets import QApplication
    assert json.loads(QApplication.clipboard().text())['cache_used'] is False
    assert not controls.timer.isActive()
    data = manager.benchmark.options()
    data.update(busy=True, status='running', completed=20, total=80)
    controls.bind(data)
    assert controls.timer.isActive() and controls.progress.value() == 20
    assert not controls.progress_text.isHidden() and controls.progress_text.text() == '20 / 80 条 · 25%'
    assert not controls.start_button.isEnabled() and not panel.automatic.isEnabled()
    # Switching the engine while measuring is refused, including manual limits.
    assert not panel.manual.isEnabled() and not panel.apply.isEnabled()
    data.update(busy=False, status='done')
    controls.bind(data)
    # ...but finishing must hand the manual configuration back, not leave it locked.
    assert panel.manual.isEnabled() and panel.apply.isEnabled() and panel.automatic.isEnabled()
    assert not controls.timer.isActive()
    window.close()


@pytest.mark.parametrize('name,languages', [('zh', {'zh'}), ('en', {'en'}), ('bilingual', {'zh', 'en'})])
def test_workload_has_twenty_distinct_sentences(name, languages):
    rows = workload(name)
    assert len(rows) == len({row['text'] for row in rows}) == 20
    assert {row['language'] for row in rows} == languages
    if name == 'bilingual':
        assert [row['language'] for row in rows] == ['zh', 'en']*10


def test_preset_options_the_current_model_never_declared_do_not_block_or_reach_a_backend(benchmark):
    manager, native, batch, _ = benchmark
    voice, _ = manager.config.resolve('qwen')
    manager.config.save_voice({**voice, 'request_options': {**voice['request_options'],
        'stream_chunk_policy': 'grow', 'stream_frames_per_event': 12, 'voice_id': 'aiden'}})
    manager.benchmark.start('qwen')
    state = finished(manager)
    assert state['status'] == 'done'
    report = state['result']
    assert report['ignored_options'] == ['stream_chunk_policy', 'stream_frames_per_event', 'voice_id']
    assert set(report['generation_options']) == {'seed', 'max_tokens'}
    assert all(set(call['voice']['request_options']) == {'seed', 'max_tokens'} for call in native.calls)
    assert all(set(call['options']) <= {'max_new_tokens', 'do_sample', 'temperature', 'top_k', 'top_p',
                                        'repetition_penalty', 'subtalker_dosample', 'subtalker_temperature',
                                        'subtalker_top_k', 'subtalker_top_p'} for call in batch.calls)
    # The saved preset keeps its stale keys; only the benchmark snapshot is cleaned.
    assert manager.config.resolve('qwen')[0]['request_options']['voice_id'] == 'aiden'


def test_option_declared_by_the_model_but_missing_from_the_batch_contract_is_still_refused(tmp_path):
    voice, settings = assets(tmp_path)
    path = tmp_path / 'custom.gguf'
    gguf(path, {'schema_version': 1, 'family': 'qwen3_tts', 'languages': ['zh'],
                'capabilities': {'clone': ['speaker_reference']},
                'options': {'request': [
                    {'name': 'max_tokens', 'type': 'int', 'default': 1024, 'min': 1, 'max': 8192},
                    {'name': 'text_chunk_size', 'type': 'int', 'default': 8192, 'min': 0, 'max': 12000},
                    {'name': 'seed', 'type': 'int', 'default': -1, 'min': -1, 'max': 2147483647},
                    {'name': 'num_inference_steps', 'type': 'int', 'default': 10, 'min': 1, 'max': 50}]}},
         config={'tts_model_type': 'base'})
    settings = settings.model_copy(update={'source_model': str(path)})
    voice.update(model_dir=str(path), device=settings.device,
                 request_options={'seed': 42, 'num_inference_steps': 10})
    kept, dropped = compatible(voice)
    assert dropped == [] and kept['num_inference_steps'] == 10
    assert 'num_inference_steps' in unsupported(voice, '你好', settings)


def test_native_only_options_declared_by_the_engine_never_block_or_reach_the_batch_backend(benchmark, tmp_path):
    manager, native, batch, _ = benchmark
    voice, _ = manager.config.resolve('qwen')
    # Mirrors audio-v0.9.x: the engine spec declares streaming transport and the
    # CustomVoice speaker table next to the sampling parameters.
    specs = [deepcopy(s) for s in FALLBACK['qwen3_tts']] + [
        {'name': 'stream_chunk_policy', 'type': 'string', 'default': 'grow', 'enum': ['grow', 'fixed']},
        {'name': 'stream_frames_per_event', 'type': 'int', 'default': 12, 'min': 1, 'max': 999},
        {'name': 'voice_id', 'type': 'string', 'default': 'aiden', 'enum': ['aiden', 'dylan']}]
    gguf(Path(voice['model_dir']), {'schema_version': 1, 'family': 'qwen3_tts', 'languages': ['zh', 'en'],
        'capabilities': {'clone': ['speaker_reference']}, 'options': {'request': specs}},
        config={'tts_model_type': 'base'})
    manager.config.save_voice({**voice, 'request_options': {**voice['request_options'],
        'stream_chunk_policy': 'grow', 'stream_frames_per_event': 12, 'voice_id': 'aiden'}})
    manager.benchmark.start('qwen')
    state = finished(manager)
    assert state['status'] == 'done'
    report = state['result']
    assert report['ignored_options'] == ['stream_chunk_policy', 'stream_frames_per_event', 'voice_id']
    # The native engine really accepts them; the batch contract forwards neither.
    assert all('stream_chunk_policy' in call['voice']['request_options'] for call in native.calls)
    assert all(not {'stream_chunk_policy', 'voice_id'} & set(call['options']) for call in batch.calls)
