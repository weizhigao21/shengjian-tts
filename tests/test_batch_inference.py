"""Real queue/HTTP/IPC with isolated audio; SDK contract tests do not fake throughput."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from voice_workbench.api import create_app
from voice_workbench.batch_inference.backend import BatchBackend
from voice_workbench.batch_inference.policy import model_signature
from voice_workbench.batch_inference.process import BatchProcess
from voice_workbench.batch_inference.qwen_worker import QwenEngine
from voice_workbench.batch_inference.schemas import BatchSettings
from voice_workbench.config import ConfigStore
from voice_workbench.manager import Manager
from test_inline_generation import index_voice
from test_model_capabilities import gguf
from test_service import ControlledRunner, make_wave, wait_job


class BatchProbe:
    def __init__(self):
        self.calls = []
        self.process = None
        self.loaded = False
        self.key = None
        self.last_used = time.monotonic()
        self.block = threading.Event()
        self.fail = False

    def generate(self, settings, signature, payload, cancelled, timeout):
        self.calls.append(deepcopy(payload))
        warm = self.process is not None
        self.process = SimpleNamespace(poll=lambda: None)
        self.key = signature
        while self.block.is_set() and not cancelled.is_set():
            time.sleep(.01)
        if cancelled.is_set():
            raise RuntimeError('cancelled')
        if self.fail:
            raise RuntimeError('CUDA out of memory')
        for item in payload['items']:
            make_wave(Path(item['output']))
        self.loaded = True
        self.last_used = time.monotonic()
        return {'ok': True, 'count': len(payload['items']), 'warm_model': warm, 'prompt_cache_hit': warm}

    def interrupt(self):
        self.process = None
        self.loaded = False
        self.key = None


def assets(tmp_path):
    voice = index_voice(tmp_path)
    path = tmp_path / 'qwen.gguf'
    gguf(path, {'schema_version': 1, 'family': 'qwen3_tts', 'languages': ['zh', 'en'],
                'capabilities': {'clone': ['speaker_reference']}}, config={'tts_model_type': 'base'})
    voice.update(id='qwen', family='qwen3_tts', model_dir=str(path), voice_mode='clone',
                 reference_text='参考原文。', request_options={'seed': 42, 'max_tokens': 64})
    model = tmp_path / 'hf-model'
    (model / 'speech_tokenizer').mkdir(parents=True)
    (model / 'config.json').write_text(json.dumps({'model_type': 'qwen3_tts', 'tts_model_type': 'base'}))
    (model / 'model.safetensors').write_bytes(b'fixture')
    (model / 'speech_tokenizer/model.safetensors').write_bytes(b'fixture')
    settings = BatchSettings(python=sys.executable, model_dir=str(model), source_model=str(path))
    return voice, settings


@pytest.fixture
def batch_manager(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    voice, settings = assets(tmp_path)
    data = tmp_path / 'data'
    ConfigStore(data).save_voice(voice)
    native = ControlledRunner()
    manager = Manager(data, native)
    manager.batch.settings = settings
    manager.batch.reason = ''
    probe = BatchProbe()
    manager.batch.process = probe
    yield manager, native, probe
    probe.block.clear()
    manager.close()


def test_bilingual_queue_uses_one_batch_backend_and_finishes_odd_tail_warm(batch_manager):
    manager, native, probe = batch_manager
    segments = [{'voice_id': 'qwen', 'text': text, 'overrides': {'language': language}} for text, language in
                [('你好', 'zh'), ('Hello', 'en'), ('欢迎', 'zh'), ('Welcome', 'en'), ('再见', 'zh')]]
    job_id = manager.submit('双语测试', 'qwen', segments)
    done = wait_job(manager.store.get, job_id, ('done',))
    assert [len(call['items']) for call in probe.calls] == [2, 2, 1]
    assert [item['language'] for item in probe.calls[0]['items']] == ['Chinese', 'English']
    assert [s['text'] for s in done['segments']] == [s['text'] for s in segments]
    assert all(s['inference']['backend'] == 'qwen3_batch' for s in done['segments'])
    assert all(s['attempts'] == 1 for s in done['segments']) and not native.calls
    original = [s['audio'] for s in done['segments']]
    repeated = manager.submit('重复批次', 'qwen', segments)
    second = wait_job(manager.store.get, repeated, ('done',))
    assert [s['audio'] for s in second['segments']] == original
    assert all(s['cached'] for s in second['segments']) and len(probe.calls) == 3
    (manager.root/'audio'/original[0]).write_bytes(b'broken')
    repaired = manager.submit('修复不完整缓存', 'qwen', segments)
    repair = wait_job(manager.store.get, repaired, ('done',))
    assert not repair['segments'][0]['cached'] and len(probe.calls) == 4
    assert len(probe.calls[-1]['items']) == 2


def test_native_opt_out_and_cold_single_keep_existing_runner(batch_manager):
    manager, native, probe = batch_manager
    first = manager.submit('指定原生', 'qwen', [{'text': 'A'}, {'text': 'B'}], inference_mode='native')
    assert wait_job(manager.store.get, first, ('done',))['completed'] == 2
    single = manager.submit('单条', 'qwen', [{'text': 'C'}])
    assert wait_job(manager.store.get, single, ('done',))['segments'][0]['inference']['backend'] == 'native'
    assert native.calls == ['A', 'B', 'C'] and not probe.calls


@pytest.mark.parametrize('count,sizes', [(2, []), (7, []), (8, [8]), (9, [8, 1]), (20, [8, 8, 4])])
def test_auto_batch_start_threshold_preserves_small_jobs_and_warm_tails(batch_manager, count, sizes):
    manager, native, probe = batch_manager
    manager.batch.settings = manager.batch.settings.model_copy(update={'max_batch_size': 8, 'min_batch_size': 8})
    identity = manager.submit('启动门槛', 'qwen', [{'text': f'row {i}'} for i in range(count)])
    job = wait_job(manager.store.get, identity, ('done',))
    assert [len(call['items']) for call in probe.calls] == sizes
    assert len(native.calls) == (0 if sizes else count)
    assert all(row['attempts'] == 1 for row in job['segments'])
    assert [row['text'] for row in job['segments']] == [f'row {i}' for i in range(count)]


def test_explicit_batch_request_can_override_automatic_start_threshold(batch_manager):
    manager, native, probe = batch_manager
    manager.batch.settings = manager.batch.settings.model_copy(update={'max_batch_size': 8, 'min_batch_size': 8})
    identity = manager.submit('主动合批', 'qwen', [{'text': 'A'}, {'text': 'B'}], inference_mode='batch')
    assert wait_job(manager.store.get, identity, ('done',))['completed'] == 2
    assert len(probe.calls) == 1 and not native.calls


def test_start_threshold_counts_only_compatible_rows_and_plan_explains_native_route(batch_manager):
    manager, native, probe = batch_manager
    manager.batch.settings = manager.batch.settings.model_copy(update={'max_batch_size': 8, 'min_batch_size': 8})
    voice, _ = manager.config.resolve('qwen')
    assert manager.batch.plan(voice, ['A']*7)['backend'] == 'native'
    assert '8' in manager.batch.plan(voice, ['A']*7)['reason']
    assert manager.batch.plan(voice, ['A']*8)['batch_sizes'] == [8]
    rows = [{'text': str(i), 'voice_id': 'qwen', 'overrides': {'request_options': {'seed': i % 2}}} for i in range(8)]
    identity = manager.submit('不同参数不足门槛', 'qwen', rows)
    assert wait_job(manager.store.get, identity, ('done',))['completed'] == 8
    assert len(native.calls) == 8 and not probe.calls


def test_batch_start_threshold_configuration_is_bounded():
    assert BatchSettings().min_batch_size == 2
    with pytest.raises(ValueError, match='启动合批条数'):
        BatchSettings(min_batch_size=8, max_batch_size=4)


def test_different_seeds_are_grouped_separately_without_crossing_generations(batch_manager):
    manager, native, probe = batch_manager
    rows = [{'text': str(i), 'voice_id': 'qwen', 'overrides': {'request_options': {'seed': seed}}}
            for i, seed in enumerate((7, 8, 7, 8))]
    identity = manager.submit('独立种子', 'qwen', rows)
    done = wait_job(manager.store.get, identity, ('done',))
    assert [[i['text'] for i in call['items']] for call in probe.calls] == [['0', '2'], ['1', '3']]
    assert [call['seed'] for call in probe.calls] == [7, 8]
    assert done['completed'] == 4 and not native.calls


def test_backend_failure_requeues_once_and_falls_back_to_native(batch_manager):
    manager, native, probe = batch_manager
    probe.fail = True
    identity = manager.submit('回退', 'qwen', [{'text': '一'}, {'text': '二'}, {'text': '三'}])
    done = wait_job(manager.store.get, identity, ('done',))
    assert len(probe.calls) == 1 and native.calls == ['一', '二', '三']
    assert [s['attempts'] for s in done['segments']] == [2, 2, 1]
    assert all(s['inference']['backend'] == 'native' and '显存' in s['inference']['fallback_reason'] for s in done['segments'])
    assert not list((manager.root / 'temporary').glob('*-batch.wav'))


def test_cancellation_is_scoped_to_one_generation_and_never_triggers_fallback(batch_manager):
    manager, _, probe = batch_manager
    probe.block.set()
    identity = manager.submit('取消前批', 'qwen', [{'text': '旧一'}, {'text': '旧二'}])
    deadline = time.monotonic() + 3
    while not probe.calls and time.monotonic() < deadline: time.sleep(.01)
    manager.submit('继续后批', 'qwen', [{'text': '新一'}, {'text': '新二'}], job_id=identity)
    manager.cancel(identity, generation=1)
    probe.block.clear()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = manager.store.get(identity)
        if [s['status'] for s in result['segments']] == ['cancelled', 'cancelled', 'done', 'done']: break
        time.sleep(.01)
    assert [s['status'] for s in result['segments']] == ['cancelled', 'cancelled', 'done', 'done']
    assert not manager.batch.failures


def test_changed_hf_assets_and_batch_composition_do_not_reuse_old_audio(batch_manager):
    manager, _, probe = batch_manager
    rows = [{'text': '一'}, {'text': '二'}]
    first = manager.submit('首批', 'qwen', rows)
    old = wait_job(manager.store.get, first, ('done',))
    model = Path(manager.batch.settings.model_dir) / 'model.safetensors'
    model.write_bytes(b'changed fixture')
    second = manager.submit('新权重', 'qwen', rows)
    new = wait_job(manager.store.get, second, ('done',))
    assert old['segments'][0]['audio'] != new['segments'][0]['audio']
    third = manager.submit('不同组合', 'qwen', [{'text': '一'}, {'text': '三'}])
    other = wait_job(manager.store.get, third, ('done',))
    assert other['segments'][0]['audio'] != new['segments'][0]['audio'] and len(probe.calls) == 3


def test_api_readiness_plan_and_legacy_submission_are_backward_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    native = ControlledRunner()
    with TestClient(create_app(tmp_path, native)) as client:
        manager = client.app.state.manager
        assert {'batch_inference', 'inference_plan'} <= set(client.get('/health').json()['capabilities'])
        before = manager.config.path.read_bytes()
        assert client.get('/inference/backends').json()['backends'][1]['ready'] is False
        plan = client.post('/inference/plan', json={'texts': ['一', '二']}).json()
        assert plan['backend'] == 'native' and plan['requires_setup']
        assert manager.config.path.read_bytes() == before and not manager.store.list()
        assert not manager.batch.process.process
        bad = client.put('/settings/batch-inference', json={'max_batch_size': 100})
        assert bad.status_code == 422
        result = client.post('/jobs', json={'segments': [{'text': '一'}, {'text': '二'}], 'inference_mode': 'batch'})
        done = wait_job(lambda identity: client.get('/jobs/'+identity).json(), result.json()['job_id'])
        assert done['completed'] == 2 and native.calls == ['一', '二']
        assert all(s['inference']['fallback_reason'] for s in done['segments'])
        assert client.post('/jobs', json={'segments': [{'text': 'x'}], 'inference_mode': 'invalid'}).status_code == 422


def test_setting_changes_are_rejected_while_batch_is_running(batch_manager):
    manager, _, probe = batch_manager
    before = manager.config.path.read_bytes()
    probe.block.set()
    identity = manager.submit('运行中', 'qwen', [{'text': '一'}, {'text': '二'}])
    deadline = time.monotonic()+3
    while not probe.calls and time.monotonic() < deadline: time.sleep(.01)
    with pytest.raises(Exception, match='正在使用'):
        manager.batch.configure(BatchSettings(enabled=False))
    assert before == manager.config.path.read_bytes() and manager.batch.settings.enabled
    manager.cancel(identity)


def test_readiness_detects_missing_dependencies_and_rejects_non_base_model(tmp_path, monkeypatch):
    voice, settings = assets(tmp_path)
    manager = SimpleNamespace(root=tmp_path, config=ConfigStore(tmp_path/'data'))
    backend = BatchBackend(manager)
    monkeypatch.setattr('voice_workbench.batch_inference.backend.subprocess.run',
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps({'missing': ['qwen_tts'], 'versions': {'torch': 'test'}})))
    assert 'qwen_tts' in backend.diagnose(settings)[0]
    config = Path(settings.model_dir)/'config.json'
    config.write_text(json.dumps({'model_type': 'qwen3_tts', 'tts_model_type': 'custom_voice'}))
    assert 'Base' in backend.diagnose(settings)[0]


def test_worker_sdk_contract_reuses_one_model_and_one_prompt_for_real_batch_lists(tmp_path, monkeypatch):
    voice, settings = assets(tmp_path)
    calls, prompts, loads, seeds = [], [], [], []
    model = SimpleNamespace(
        create_voice_clone_prompt=lambda **kw: prompts.append(kw) or ['cached-prompt'],
        generate_voice_clone=lambda **kw: (calls.append(kw) or [[0], [0]], 24000))
    torch = SimpleNamespace(float32='float32', cuda=SimpleNamespace(is_available=lambda: False),
        inference_mode=nullcontext, set_num_threads=lambda _: None, manual_seed=seeds.append)
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(write=lambda path, *a, **kw: make_wave(Path(path))))
    monkeypatch.setitem(sys.modules, 'qwen_tts', SimpleNamespace(Qwen3TTSModel=SimpleNamespace(
        from_pretrained=lambda *a, **kw: loads.append((a, kw)) or model)))
    message = {'settings': settings.model_copy(update={'device':'cpu'}).model_dump(), 'voice': voice,
        'seed': 42, 'options': {'max_new_tokens': 64, 'subtalker_dosample': False},
        'items': [{'text': '你好', 'language': 'Chinese', 'output': str(tmp_path/'一.wav')},
                  {'text': 'Hello', 'language': 'English', 'output': str(tmp_path/'二.wav')}]}
    engine = QwenEngine()
    first, second = engine.run(message), engine.run(message)
    assert len(loads) == len(prompts) == 1 and len(calls) == 2
    assert loads[0][1]['local_files_only'] and loads[0][1]['attn_implementation'] == 'sdpa'
    assert calls[0]['text'] == ['你好', 'Hello'] and calls[0]['language'] == ['Chinese', 'English']
    assert calls[0]['voice_clone_prompt'] is calls[1]['voice_clone_prompt']
    assert seeds == [42, 42] and not first['prompt_cache_hit'] and second['prompt_cache_hit']
    assert (tmp_path/'一.wav').is_file() and (tmp_path/'二.wav').is_file()


def test_real_worker_ipc_is_persistent_and_cancellable_without_torch(tmp_path, monkeypatch):
    folder = tmp_path/'voice_workbench/batch_inference'
    folder.mkdir(parents=True)
    worker = folder/'qwen_worker.py'
    worker.write_text("import sys,json,time\nn=0\nfor line in sys.stdin:\n p=json.loads(line);n+=1\n if p.get('block'): time.sleep(30)\n print(json.dumps({'ok':True,'count':n}),flush=True)\n")
    monkeypatch.setattr('voice_workbench.batch_inference.process.resource_root', lambda: tmp_path)
    process = BatchProcess(tmp_path)
    settings = BatchSettings(python=sys.executable)
    event = threading.Event()
    try:
        assert process.generate(settings, 'one', {}, event, 3)['count'] == 1
        identity = process.process.pid
        assert process.generate(settings, 'one', {}, event, 3)['count'] == 2
        assert process.process.pid == identity
        with pytest.raises(Exception, match='超时'):
            process.generate(settings, 'one', {'block': True}, event, .2)
        assert process.process is None
    finally:
        process.interrupt()


def test_a_job_with_rows_still_waiting_keeps_the_batch_worker_resident(batch_manager, monkeypatch):
    """Unloading between two batches of one job turned every later batch cold."""
    manager, native, probe = batch_manager
    assert manager.batch_idle() is True and manager.store.pending() is False
    probe.block.set()
    job_id = manager.submit('两批', 'qwen', [{'text': f'第{i}句'} for i in range(6)])
    for _ in range(200):
        if probe.calls:
            break
        time.sleep(.02)
    assert probe.calls
    assert manager.store.pending() is True
    assert manager.batch_idle() is False
    probe.block.clear()
    wait_job(manager.store.get, job_id, ('done',))
    assert manager.store.pending() is False
    assert manager.batch_idle() is True
    # The gap between two batches of one job looks idle to `active` alone.
    monkeypatch.setattr(manager.store, 'pending', lambda: True)
    assert manager.batch_idle() is False


def test_the_guard_bounds_the_wait_for_a_first_frame_not_the_whole_batch():
    from voice_workbench.batch_inference.process import COLD_ALLOWANCE, FIRST_FRAME, batch_budget, batch_stall
    # A batch that is producing frames is judged by its pace, not by the clock.
    assert batch_budget(600) == FIRST_FRAME
    assert batch_budget(600, loaded=False) == FIRST_FRAME + COLD_ALLOWANCE
    assert batch_stall(600) == 90
    # The user's preset stays the ceiling for both.
    assert batch_budget(20) == 20 and batch_budget(20, loaded=False) == 20 and batch_stall(30) == 30


def test_the_stall_guard_is_told_whether_the_worker_is_already_warm(monkeypatch):
    """A cold worker loads its weights first, so the guard must know which one it is."""
    seen = []
    monkeypatch.setattr('voice_workbench.batch_inference.process.batch_budget',
                        lambda timeout, loaded=True: seen.append(loaded) or 5)
    process = BatchProcess(Path('.'))

    class Pipe:
        def write(self, _): pass
        def flush(self): pass

    answers = queue.Queue()
    answers.put({'ok': True})
    answers.put({'ok': True})
    monkeypatch.setattr(process, 'start', lambda settings, signature: (SimpleNamespace(stdin=Pipe()), answers))
    for warm in (False, True):
        assert process.loaded is warm
        assert process.generate(BatchSettings(python=sys.executable), 'one',
                                {'items': [{'text': '一条文本'}]}, threading.Event(), 600) == {'ok': True}
    assert seen == [False, True]


def stall_worker(tmp_path, monkeypatch, body, first=.8, stall=.4):
    folder = tmp_path/'voice_workbench/batch_inference'
    folder.mkdir(parents=True)
    (folder/'qwen_worker.py').write_text(body)
    monkeypatch.setattr('voice_workbench.batch_inference.process.resource_root', lambda: tmp_path)
    monkeypatch.setattr('voice_workbench.batch_inference.process.batch_budget', lambda timeout, loaded=True: first)
    monkeypatch.setattr('voice_workbench.batch_inference.process.batch_stall', lambda timeout: stall)
    return BatchProcess(tmp_path)


def test_frames_arriving_keep_a_slow_batch_alive_past_its_first_frame_budget(tmp_path, monkeypatch):
    """A batch that is still drawing frames is slow, not wedged, and must not be killed."""
    process = stall_worker(tmp_path, monkeypatch, "import json,sys,time\n"
                          "for line in sys.stdin:\n"
                          " for n in range(1, 11):\n"
                          "  print(json.dumps({'progress': n}), flush=True)\n"
                          "  time.sleep(.1)\n"
                          " print(json.dumps({'ok': True, 'count': 1, 'frames': 10}), flush=True)\n")
    try:
        started = time.monotonic()
        answer = process.generate(BatchSettings(python=sys.executable), 'one',
                                  {'items': [{'text': '一条文本'}]}, threading.Event(), 600)
        assert answer['count'] == 1 and answer['frames'] == 10
        assert time.monotonic() - started > .9      # well past the .8 s first-frame budget
    finally:
        process.interrupt()


def test_a_batch_that_stops_drawing_frames_is_dropped_and_says_how_far_it_got(tmp_path, monkeypatch):
    process = stall_worker(tmp_path, monkeypatch, "import json,sys,time\n"
                          "for line in sys.stdin:\n"
                          " for n in (1, 2, 3): print(json.dumps({'progress': n}), flush=True)\n"
                          " time.sleep(30)\n")
    try:
        with pytest.raises(Exception, match='已渲染 3 帧'):
            process.generate(BatchSettings(python=sys.executable), 'one',
                             {'items': [{'text': '一条文本'}]}, threading.Event(), 600)
        assert process.process is None
    finally:
        process.interrupt()


def test_a_runaway_line_cannot_drag_a_batch_to_the_voice_token_cap():
    from voice_workbench.batch_inference.policy import token_budget
    # 20 characters is 57 frames of speech; the cap leaves room for pauses, not for
    # the 1024-frame preset, which is 85 seconds of audio for a five second line.
    assert token_budget(['看来封口还不太够呢', '既然已经被人知道了 那也没必要再隐瞒了吧'], 1024) == 174
    assert token_budget(['短句'], 1024) >= 96
    assert token_budget(['字' * 4000], 1024) == 1024      # never above the preset
    assert token_budget(['短句'], 64) == 64               # nor above a tighter voice
    assert token_budget([], 1024) >= 96


def test_a_batch_cannot_decode_past_what_its_longest_line_needs(batch_manager):
    """The preset is 1024 frames; a five second line needs about sixty."""
    manager, _, probe = batch_manager
    voice = next(v for v in manager.config.voices() if v['id'] == 'qwen')
    manager.config.save_voice({**voice, 'request_options': {'seed': 42, 'max_tokens': 1024}})
    texts = ['看来封口还不太够呢', '既然已经被人知道了 那也没必要再隐瞒了吧']
    identity = manager.submit('令牌上限', 'qwen', [{'text': text} for text in texts])
    wait_job(manager.store.get, identity, ('done',))
    assert probe.calls[0]['options']['max_new_tokens'] == 174


def test_stalled_batch_is_dropped_on_the_estimated_budget_not_the_preset_timeout(tmp_path, monkeypatch):
    folder = tmp_path/'voice_workbench/batch_inference'
    folder.mkdir(parents=True)
    (folder/'qwen_worker.py').write_text("import sys,time\nfor line in sys.stdin:\n time.sleep(30)\n")
    monkeypatch.setattr('voice_workbench.batch_inference.process.resource_root', lambda: tmp_path)
    monkeypatch.setattr('voice_workbench.batch_inference.process.batch_budget', lambda timeout, loaded=True: .3)
    process = BatchProcess(tmp_path)
    try:
        started = time.monotonic()
        with pytest.raises(Exception, match='超时'):
            process.generate(BatchSettings(python=sys.executable), 'one',
                             {'items': [{'text': '一条卡住的文本'}]}, threading.Event(), 600)
        assert time.monotonic() - started < 5 and process.process is None
    finally:
        process.interrupt()


def test_batch_limits_and_idle_release_keep_model_residency_bounded(batch_manager):
    manager, _, probe = batch_manager
    manager.batch.settings = manager.batch.settings.model_copy(update={'max_batch_chars': 100})
    voice = next(v for v in manager.config.voices() if v['id'] == 'qwen')
    assert manager.batch.plan(voice, ['一'*60, '二'*60])['backend'] == 'native'
    identity = manager.submit('限制', 'qwen', [{'text': '一'*60}, {'text': '二'*60}, {'text': '三'*30}])
    assert wait_job(manager.store.get, identity, ('done',))['completed'] == 3
    assert all(sum(len(item['text']) for item in call['items']) <= 100 for call in probe.calls)
    assert [len(call['items']) for call in probe.calls] == [2, 1]
    probe.last_used -= 400
    manager.batch.expire()
    assert probe.process is None


def test_missing_queued_reference_does_not_crash_scheduler(batch_manager):
    manager, native, _ = batch_manager
    with manager.lock:
        identity = manager.submit('缺失音频', 'qwen', [{'text': '一'}, {'text': '二'}])
        source = Path(manager.store.get(identity)['voice']['reference_audio'])
        source.unlink()
    result = wait_job(manager.store.get, identity)
    assert result['status'] == 'failed' and result['failed'] == 2
    assert manager.thread.is_alive() and not native.calls


def test_batch_settings_ui_applies_config_without_replacing_dirty_input(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        window = Window(18992, tmp_path, autostart=False)
        qtbot.addWidget(window)
        for timer in (window.timer, window.settings.timer, window.document_timer): timer.stop()
        def call(action, success=None, failure=None):
            try:
                result = action(client)
                if success: success(result)
            except Exception as exc:
                if failure: failure(str(exc))
                else: raise
        window.call = call
        control = window.settings.batch_inference
        control.refresh()
        assert '选择' in control.status.text()
        control.python.setText('my-edited-python.exe')
        control.refresh()
        assert control.python.text() == 'my-edited-python.exe'
        control.enabled.setChecked(False)
        assert control.predictor_fastpath.isChecked()
        assert not control.predictor_cuda_graph.isChecked()
        control.predictor_fastpath.setChecked(False)
        assert not control.predictor_cuda_graph.isEnabled()
        control.save()
        saved = client.get('/settings/batch-inference').json()['settings']
        assert saved['enabled'] is False and saved['predictor_fastpath'] is False
        assert saved['predictor_cuda_graph'] is False
        assert '关闭' in control.status.text() and control.isEnabled()
        window.close()


def test_optional_setup_plans_isolated_commands_without_creating_files(tmp_path):
    from scripts.setup_qwen_batch import commands
    args = SimpleNamespace(target=tmp_path/'runtime', device='cuda', download_model='1.7B', model_dir=tmp_path/'model')
    python, plan = commands(args)
    assert not args.target.exists() and not args.model_dir.exists()
    assert plan[0][1:3] == ['-m', 'venv']
    assert any('qwen-tts==0.1.1' in command for command in plan)
    assert plan[-1][-2] == 'Qwen/Qwen3-TTS-12Hz-1.7B-Base'
    assert 'revision=revision' in plan[-1][2]


def test_optional_setup_refuses_existing_program_files(tmp_path):
    from scripts.setup_qwen_batch import commands
    original = tmp_path/'original.txt'; original.write_text('keep')
    args = SimpleNamespace(target=tmp_path, device='cpu', download_model=None, model_dir=None)
    with pytest.raises(ValueError, match='其他文件'):
        commands(args)
    assert original.read_text() == 'keep'


def test_batch_workflow_audio_is_delivered_and_cleaned_without_losing_backend_metadata(batch_manager):
    manager, _, probe = batch_manager
    identity = manager.submit('批量交付', 'qwen', [{'text': '一'}, {'text': '二'}],
                              workflow_key='tensor-delivery', record_only=True, delivery_ack=True)
    result = wait_job(manager.store.get, identity, ('done',))
    files = [row['audio'] for row in result['segments']]
    records = manager.store.workflow_records()
    assert all(row['inference']['backend'] == 'qwen3_batch' for row in records)
    assert all((manager.workflow_audio.root/name).is_file() for name in files)
    assert not list((manager.root/'audio').glob('*.wav'))
    for name in files:
        assert manager.workflow_audio.delivered(name)['released']
    assert not any((manager.workflow_audio.root/name).exists() for name in files)
    assert all(row['audio_release_reason'] == 'delivered' for row in manager.store.workflow_records())
