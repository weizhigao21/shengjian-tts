"""Exercise the installed workflow entry without importing its GUI or live caches."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import time

from fastapi.testclient import TestClient
import pytest

from integration import local_voice_stream as stream
from test_batch_inference import batch_manager
from test_service import make_wave
from test_workflow_client import Session
from voice_workbench.api import create_app


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    modules = {}
    for name in ('tts_utils', 'tts_profile', 'tts_worker'):
        module = ModuleType('integration.' + name)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        modules[name] = module
    modules['tts_utils'].generate_filename = lambda index, *_: str(tmp_path / f'{index}.wav')
    modules['tts_utils']._get_session = lambda: None
    modules['tts_utils']._download_audio = lambda *_: None
    modules['tts_profile'].api_cache_model = lambda *args: 'voice-version'
    modules['tts_worker'].format_eta = lambda seconds: str(seconds)
    worker = SimpleNamespace(stop_flag=False, is_paused=lambda: False, _read_failures=0,
                             workflow_context={'key': 'whole-run', 'title': '整批测试'})
    for name in ('log', 'progress', 'weight_progress', 'eta'):
        signal = SimpleNamespace(values=[])
        signal.emit = signal.values.append
        setattr(worker, name + '_signal', signal)
    worker.finished = []
    worker._finish_generation = lambda ok, folder: worker.finished.append((ok, folder))
    worker.audio_cache = SimpleNamespace(get_cached_audio=lambda *args: None,
                                        save_audio_cache=lambda *args, **kwargs: None)
    api = {'url': 'http://testserver', 'model': 'qwen', 'name': '声笺'}
    return worker, api, modules


def test_whole_intake_reaches_qwen_batch_and_downloads_every_result(workflow, batch_manager, tmp_path):
    worker, api, modules = workflow
    manager, native, probe = batch_manager
    manager.batch.settings = manager.batch.settings.model_copy(update={'max_batch_size': 8, 'min_batch_size': 8})
    app = create_app(manager.root)
    app.state.manager = manager
    # The manager fixture owns lifecycle; this client exercises actual HTTP routes.
    client = TestClient(app)
    modules['tts_utils']._get_session = lambda: Session(client)
    modules['tts_utils']._download_audio = lambda url, target: Path(target).write_bytes(client.get(url).content)
    tasks = [(i, '', f'line {i}', str(tmp_path), 0) for i in range(1, 21)]
    try:
        stream.run_incremental(worker, api, tasks, 0, 0, time.time(), 'task', str(tmp_path))
    finally:
        client.close()
    assert [len(call['items']) for call in probe.calls] == [8, 8, 4]
    assert not native.calls and len(manager.store.list(include_workflow=True)) == 1
    assert worker.progress_signal.values == list(range(1, 21))
    assert worker.weight_progress_signal.values[-1] == sum(len(t[2]) for t in tasks)
    assert worker.finished == [(True, str(tmp_path))]
    assert all((tmp_path / f'{i}.wav').is_file() for i in range(1, 21))
    assert not list(manager.workflow_audio.root.glob('*.wav'))


def test_cache_hits_and_out_of_order_callbacks_update_progress_once(workflow, tmp_path, monkeypatch):
    worker, api, _ = workflow
    cached = tmp_path / 'cached.wav'
    make_wave(cached)
    worker.audio_cache.get_cached_audio = lambda text, *_: str(cached) if text == 'cached' else None
    submitted = []

    def generate(*args, on_result, **kwargs):
        submitted.extend(args[3])
        result = [(False, '第 2 段：失败'), (True, '完成：3')]
        on_result(1, result[1])
        on_result(0, result[0])
        return result

    monkeypatch.setattr(stream, 'generate_local_tasks', generate)
    tasks = [(i, '', text, str(tmp_path), 0) for i, text in enumerate(('cached', 'bad', 'good'), 1)]
    stream.run_incremental(worker, api, tasks, 5, 10, time.time(), 'task', str(tmp_path))
    assert [t[2] for t in submitted] == ['bad', 'good']
    assert worker.progress_signal.values == [6, 7, 8]
    assert worker.weight_progress_signal.values[-1] == 10 + sum(len(t[2]) for t in tasks)
    assert (tmp_path / '1.wav').read_bytes() == cached.read_bytes()
    assert worker.finished == [(False, str(tmp_path))]


@pytest.mark.parametrize('cached_contents', [b'invalid', 'truncated'])
def test_invalid_cache_is_regenerated(workflow, tmp_path, monkeypatch, cached_contents):
    worker, api, _ = workflow
    cached = tmp_path / 'cached.wav'
    make_wave(cached)
    cached.write_bytes(cached.read_bytes()[:-2] if cached_contents == 'truncated' else cached_contents)
    worker.audio_cache.get_cached_audio = lambda *_: str(cached)
    submitted = []

    def generate(*args, **kwargs):
        submitted.extend(args[3])
        return [(True, '完成')]

    monkeypatch.setattr(stream, 'generate_local_tasks', generate)
    tasks = [(1, '', 'hello', str(tmp_path), 0)]
    stream.run_incremental(worker, api, tasks, 0, 0, time.time(), 'task', str(tmp_path))
    assert submitted == tasks and worker.finished == [(True, str(tmp_path))]
    assert not (tmp_path / '1.wav').exists() and not list(tmp_path.glob('.tts-*'))


def test_large_intake_splits_only_at_http_limit_and_stop_prevents_next_submission(workflow, tmp_path, monkeypatch):
    worker, api, _ = workflow
    submitted = []

    def generate(*args, on_result, **kwargs):
        submitted.append(args[3])
        for index in range(len(args[3])):
            on_result(index, (True, '完成'))
        return [(True, '完成')] * len(args[3])

    monkeypatch.setattr(stream, 'generate_local_tasks', generate)
    tasks = [(i, '', 'a', str(tmp_path), 0) for i in range(10001)]
    stream.run_incremental(worker, api, tasks, 0, 0, time.time(), 'task', str(tmp_path))
    assert [len(group) for group in submitted] == [10000, 1]
    assert worker.progress_signal.values[-1] == 10001 and worker.finished[-1][0]

    def stop(*args, **kwargs):
        submitted.append(args[3])
        worker.stop_flag = True
        return [(False, '已取消')] * len(args[3])

    submitted.clear()
    monkeypatch.setattr(stream, 'generate_local_tasks', stop)
    stream.run_incremental(worker, api, tasks, 0, 0, time.time(), 'task', str(tmp_path))
    assert [len(group) for group in submitted] == [10000]
    assert worker.finished[-1] == (False, str(tmp_path))
