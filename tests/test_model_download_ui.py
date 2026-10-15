"""Catalog categories, a persistent transfer queue and measured network rates."""
from copy import deepcopy
import hashlib
import threading

import httpx
import pytest
from PyQt6.QtCore import Qt

from voice_workbench.downloads import download_file, transfer_state
from voice_workbench.desktop.settings.model_categories import included_model
from voice_workbench.gui import Window


def model(identity, tasks, **values):
    return dict(id=identity, name=identity, description='模型功能说明', precision='Q8', family=identity,
                tasks=tasks, size=4*1024**2, sha256='0'*64, status='available', installed=False,
                downloadable=True, adapted=bool(set(tasks) & {'tts', 'clone'}), **values)


@pytest.mark.parametrize('category,expected', [
    ('all', {'voice', 'asr', 'aligner', 'editor', 'music', 'mixed'}),
    ('tts', {'voice', 'mixed'}), ('stt', {'asr', 'mixed'}), ('align', {'aligner'}),
    ('edit', {'editor'}), ('other', {'music'}), ('adapted', {'voice', 'mixed'}),
])
def test_categories_follow_engine_tasks_and_multi_purpose_models(category, expected):
    entries = [model('voice', ['tts']), model('asr', ['asr']), model('aligner', ['align']),
               model('editor', ['vc']), model('music', ['music']), model('mixed', ['tts', 'stt'])]
    assert {m['id'] for m in entries if included_model(m, category)} == expected


def test_categories_cover_local_legacy_entries_without_task_metadata():
    assert included_model({'adapted': True}, 'tts')
    assert included_model({'tasks': [], 'adapted': False, 'edit_adapted': True}, 'edit')
    assert included_model({'adapted': False, 'recognition_adapted': True}, 'stt')
    assert included_model({'adapted': False, 'align_adapted': True}, 'align')
    assert not included_model({'adapted': False, 'align_adapted': True}, 'other')


@pytest.fixture
def panel(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(Window, 'call', lambda *args, **kwargs: None)
    window = Window(18994, tmp_path, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.settings.timer, window.document_timer):
        timer.stop()
    window.ready = True
    window.set_page(3)
    panel = window.settings
    panel.model_data = {'installed': [], 'catalog': [model('voice', ['tts']), model('asr', ['asr'])]}
    panel.render_models()
    window.resize(1080, 720)
    window.show()
    yield panel
    window.close()


def test_category_tabs_filter_the_catalog_from_below_its_heading(panel, qtbot):
    assert not panel.model_downloads.isVisible()
    assert panel.model_filter.geometry().top() > panel.catalog_label.geometry().bottom()
    index = panel.model_filter.findData('stt')
    qtbot.mouseClick(panel.model_filter, Qt.MouseButton.LeftButton, pos=panel.model_filter.tabRect(index).center())
    assert panel.model_filter.currentData() == 'stt'
    assert [m['id'] for m in panel.visible_catalog] == ['asr']
    panel.model_search.setText('no such model')
    assert panel.catalog_table.rowCount() == 0


def test_download_area_survives_filters_and_updates_in_place_then_disappears(panel):
    task = panel.model_data['catalog'][0]
    task.update(status='downloading', received=1024**2, speed_bps=2.5*1024**2)
    panel.render_models()
    assert panel.model_downloads.isVisible()
    assert panel.downloads_table.rowCount() == 1
    assert '25.0%' in panel.downloads_table.item(0, 1).text()
    assert panel.downloads_table.item(0, 2).text() == '2.50 MB/s'
    assert panel.downloads_table.cellWidget(0, 4).text() == '暂停'
    assert 'voice' not in [m['id'] for m in panel.visible_catalog]
    item = panel.downloads_table.item(0, 0)
    action = panel.downloads_table.cellWidget(0, 4)
    panel.model_filter.setCurrentIndex(panel.model_filter.findData('stt'))
    panel.model_search.setText('asr')
    assert panel.model_downloads.isVisible()
    assert panel.downloads_table.item(0, 0) is item
    assert panel.downloads_table.cellWidget(0, 4) is action
    task.update(received=2*1024**2, speed_bps=512*1024)
    panel.render_models()
    assert panel.downloads_table.item(0, 2).text() == '512.0 KB/s'
    assert panel.downloads_table.item(0, 0) is item
    task['status'] = 'paused'
    panel.render_models()
    assert panel.model_downloads.isVisible()
    assert panel.downloads_table.item(0, 2).text() == '—'
    assert panel.downloads_table.cellWidget(0, 4).text() == '继续'
    task['status'] = 'done'
    panel.render_models()
    assert not panel.model_downloads.isVisible() and panel.downloads_table.rowCount() == 0


def test_transfer_actions_use_the_matching_download_or_update_and_can_retry(panel, monkeypatch):
    calls = []
    monkeypatch.setattr(panel, 'pause_download', lambda m: calls.append(('pause', m['id'])))
    monkeypatch.setattr(panel, 'download_model', lambda m: calls.append(('download', m['id'])))
    monkeypatch.setattr(panel, 'update_model', lambda m: calls.append(('update', m['id'])))
    task = panel.model_data['catalog'][0]
    task.update(status='downloading', received=100)
    panel.render_models(); panel.downloads_table.cellWidget(0, 4).click()
    task['status'] = 'pausing'
    panel.render_models()
    assert not panel.downloads_table.cellWidget(0, 4).isEnabled()
    task['status'] = 'failed'; task['error'] = '网络中断'
    panel.render_models(); panel.downloads_table.cellWidget(0, 4).click()
    assert panel.downloads_table.cellWidget(0, 4).text() == '重试'
    assert panel.downloads_table.item(0, 3).toolTip() == '网络中断'
    task.update(status='paused', update=True, installed=True)
    panel.model_data['installed'] = [{**task, 'id': 'installed-voice', 'catalog_id': task['id'],
                                     'path': 'model.gguf', 'exists': True, 'preset_count': 0}]
    panel.render_models(); panel.downloads_table.cellWidget(0, 4).click()
    assert calls == [('pause', 'voice'), ('download', 'voice'), ('update', 'installed-voice')]


@pytest.mark.parametrize('accept_range', [True, False])
def test_speed_measures_new_bytes_when_resuming_or_restarting(tmp_path, monkeypatch, accept_range):
    now = [10.0]
    monkeypatch.setattr('voice_workbench.downloads.monotonic', lambda: now[0])
    chunk_size = 262144
    content = b'x' * (4*chunk_size)
    target = tmp_path/'weights.gguf'
    target.with_suffix('.gguf.part').write_bytes(content[:chunk_size])
    entry = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(chunk_size if accept_range else 0, len(content), chunk_size):
                now[0] += 1
                yield content[offset:offset+chunk_size]
    def handle(request):
        assert request.headers['Range'] == f'bytes={chunk_size}-'
        headers = {'Content-Range': f'bytes {chunk_size}-{len(content)-1}/{len(content)}'} if accept_range else {}
        return httpx.Response(206 if accept_range else 200, headers=headers, stream=Stream())
    snapshots, state = [], {}
    def progress(**changes):
        state.update(changes); snapshots.append(deepcopy(state))
    download_file(target, entry, 'https://example.test/model', threading.Event(),
                  lambda: httpx.Client(transport=httpx.MockTransport(handle)), progress)
    samples = [s for s in snapshots if s.get('speed_bps', 0) > 0]
    assert samples and all(s['speed_bps'] == chunk_size for s in samples)
    assert state['resumed_from'] == (chunk_size if accept_range else 0)
    assert state['status'] == 'verifying' and state['speed_bps'] == 0
    assert target.read_bytes() == content
    assert not target.with_suffix('.gguf.part').exists()


def test_public_rate_expires_during_a_stall_and_never_survives_a_pause(monkeypatch):
    monkeypatch.setattr('voice_workbench.downloads.monotonic', lambda: 100)
    state = {'status': 'downloading', 'received': 1000, 'speed_bps': 200, 'speed_sampled_at': 99}
    assert transfer_state(state)['speed_bps'] == 200
    assert 'speed_sampled_at' not in transfer_state(state)
    state['speed_sampled_at'] = 96
    assert transfer_state(state)['speed_bps'] == 0
    state.update(status='paused', speed_sampled_at=99)
    assert transfer_state(state)['speed_bps'] == 0
