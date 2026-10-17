"""Observed backend status through queue execution, caching, fallback and desktop polling."""
import time
import pytest

from test_batch_inference import batch_manager
from test_service import wait_job
from voice_workbench.desktop.components.inference_status import record_engine, status_presentation
from voice_workbench.gui import Window


def wait_status(manager, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = manager.status()
        if predicate(state):
            return state
        time.sleep(.01)
    raise AssertionError(state)


def test_real_active_backend_cache_and_last_native_replace_old_batch(batch_manager):
    manager, native, probe = batch_manager
    probe.block.set()
    rows = [{'text': '一'}, {'text': '二'}]
    identity = manager.submit('状态', 'qwen', rows)
    state = wait_status(manager, lambda s: probe.calls and s['active'][0]['phase'] == 'generating')
    active = state['active'][0]
    assert active['backend'] == 'qwen3_batch' and active['indices'] == [1, 2]
    assert active['batch_size'] == 2 and active['job_id'] == identity
    assert '本批 2 条' in status_presentation(state)[0]
    probe.block.clear()
    wait_job(manager.store.get, identity, ('done',))
    state = wait_status(manager, lambda s: not s['active'])
    assert state['last_execution']['backend'] == 'qwen3_batch'
    assert not state['last_execution']['cached']
    assert '空闲' in status_presentation(state)[0]
    repeated = manager.submit('缓存', 'qwen', rows)
    wait_job(manager.store.get, repeated, ('done',))
    state = wait_status(manager, lambda s: not s['active'])
    assert state['last_execution']['cached'] and len(probe.calls) == 1
    assert '缓存命中' in status_presentation(state)[0]
    native.block.set()
    recent = manager.submit('指定原生', 'qwen', [{'text': '三'}], inference_mode='native')
    state = wait_status(manager, lambda s: native.calls and s['active'][0]['phase'] == 'generating')
    assert state['active'][0]['backend'] == 'native'
    assert '指定原生' in state['active'][0]['route_reason']
    assert '原生引擎' in status_presentation(state)[0]
    native.block.clear()
    wait_job(manager.store.get, recent, ('done',))
    state = wait_status(manager, lambda s: not s['active'])
    assert state['last_execution']['backend'] == 'native'
    assert state['last_execution']['job_id'] == recent
    # Stale last batch metadata cannot turn a completed native request into batch.
    assert manager.batch.last_result['job_id'] == identity or manager.batch.last_result['job_id'] == repeated
    assert '上次原生' in status_presentation(state)[0]


def test_failed_batch_displays_actual_native_fallback_and_cancellation(batch_manager):
    manager, native, probe = batch_manager
    probe.fail = True
    native.block.set()
    identity = manager.submit('回退', 'qwen', [{'text': '一'}, {'text': '二'}])
    state = wait_status(manager, lambda s: native.calls and s['active'][0]['phase'] == 'generating')
    assert state['active'][0]['backend'] == 'native'
    text, detail, color = status_presentation(state)
    assert '已回退' in text and '显存' in detail and color == 'fallback'
    manager.cancel(identity)
    wait_job(manager.store.get, identity, ('cancelled',))
    state = wait_status(manager, lambda s: not s['active'])
    assert state['last_execution']['status'] == 'cancelled'
    assert '生成中' not in status_presentation(state)[0]


@pytest.mark.parametrize('health,text', [
    (None, '未连接'),
    ({'inference': {'ready': True}}, '批量已就绪'),
    ({'inference': {'loaded': True}}, '批量模型驻留'),
    ({'inference': {'ready': False, 'reason': '未安装'}}, '原生引擎'),
    ({'recognition': {'id': 'x'}}, '语音识别处理中'),
    ({'audio_editing': {'id': 'x'}}, '音频编辑处理中'),
    ({'inference_benchmark': {'busy': True, 'message': '批量后端 · 热模型', 'completed': 8, 'total': 80}}, '速度测试中'),
    ({'active': [{'backend': 'native', 'phase': 'checking'}]}, '准备中'),
    ({'active': [{'backend': 'qwen3_batch', 'phase': 'generating', 'batch_size': 8}]}, '本批 8 条'),
    ({'active': [{'backend': 'native', 'phase': 'generating', 'cancelling': True}]}, '取消中'),
])
def test_status_distinguishes_ready_idle_and_observed_work(health, text):
    assert text in status_presentation(health)[0]


def test_record_metadata_does_not_guess_legacy_backend_or_call_cache_inference():
    assert record_engine({'status': 'done'})[0] == '未记录'
    assert record_engine({'status': 'queued'})[0] == '—'
    row = {'status': 'done', 'inference': {'backend': 'qwen3_batch', 'batch_size': 8}}
    assert record_engine(row)[0] == '批量 ×8'
    cached = dict(row, cached=True)
    text, detail = record_engine(cached)
    assert text == '缓存' and '未执行推理' in detail and '8 条' in detail
    assert record_engine({'status': 'done', 'inference': {'backend': 'native', 'fallback_reason': '显存不足'}})[0] == '原生 · 回退'


def test_workspace_badge_visible_on_article_and_polling_disconnect_clears_it(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(Window, 'call', lambda *a, **k: None)
    window = Window(18998, tmp_path, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.document_timer, window.settings.timer):
        timer.stop()
    window.show()
    window.set_page(0)
    assert window.inference_status.isVisible()
    window.inference_status.bind({'active': [{'backend': 'qwen3_batch', 'batch_size': 8, 'phase': 'generating', 'job_id': 'current'}]})
    assert '本批 8 条' in window.inference_status.text()
    assert 'current' in window.inference_status.toolTip()
    window.set_page(2)
    assert window.inference_status.isVisible()
    window.set_service_state('未连接', 'failed')
    assert '未连接' in window.inference_status.text()
    assert '本批' not in window.inference_status.text()
    assert window.segments_table.columnCount() == 7
    assert window.segments_table.horizontalHeaderItem(6).text() == '推理引擎'
    window.close()


def test_compact_benchmark_status_does_not_serialize_old_reports(batch_manager):
    manager, _, _ = batch_manager
    class NoCopy:
        def __deepcopy__(self, memo):
            raise AssertionError('copied report during health poll')
    previous = manager.benchmark.result
    manager.benchmark.result = NoCopy()
    try:
        assert 'result' not in manager.status()['inference_benchmark']
    finally:
        manager.benchmark.result = previous


def test_history_engine_column_uses_record_not_current_backend(batch_manager, qtbot, monkeypatch):
    manager, _, _ = batch_manager
    identity = manager.submit('历史批量', 'qwen', [{'text': '一'}, {'text': '二'}])
    original = wait_job(manager.store.get, identity, ('done',))
    repeated = manager.submit('缓存', 'qwen', [{'text': '一'}, {'text': '二'}])
    cached = wait_job(manager.store.get, repeated, ('done',))
    monkeypatch.setattr(Window, 'call', lambda *a, **k: None)
    window = Window(18997, manager.root, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.document_timer, window.settings.timer):
        timer.stop()
    window.inference_status.bind({'active': [{'backend': 'native', 'phase': 'generating', 'job_id': 'another-work'}]})
    for job, expected in ((original, '批量 ×2'), (cached, '缓存')):
        window.selected_job = job['id']
        window.render_job(job, force=True)
        assert window.segments_table.item(0, 6).text() == expected
        assert 'Qwen' in window.segments_table.item(0, 6).toolTip()
        assert '原生引擎' in window.inference_status.text()
    window.close()
