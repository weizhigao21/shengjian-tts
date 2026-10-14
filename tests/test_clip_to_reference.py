"""The work's generation-history menu imports the clicked record, not the selected one."""
from pathlib import Path

import pytest
from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication, QMenu

from voice_workbench.library import ReferenceDialog
from test_audio_to_reference import prepare
from test_dialogue_scope import environment
from test_library_and_deletion import wait_job


def show_work(client, window):
    job = client.get('/jobs').json()[0]
    window.selected_job = job['id']
    window.set_page(0)
    window.resume_creation_editor()
    window.open_job_data(client.get('/jobs/' + job['id']).json())
    window.audio_output.setMuted(True)
    window.toggle_clip_list()
    QApplication.processEvents()


def open_context(window, qtbot, row=1, choose=True, refresh=None):
    enabled = []
    def select():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        try:
            action = next(a for a in menu.actions() if a.text() == '保存到声音库…')
            enabled.append(action.isEnabled())
            if refresh:
                refresh()
            if choose:
                menu.setActiveAction(action)
                qtbot.keyClick(menu, Qt.Key.Key_Return)
        finally:
            menu.close()
    table = window.segments_table
    position = table.visualItemRect(table.item(row, 2)).center()
    QTimer.singleShot(0, select)
    QApplication.sendEvent(table.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position,
                                                             table.viewport().mapToGlobal(position)))
    return enabled


def test_clicked_history_survives_reordering_cancel_and_original_deletion(tmp_path, qtbot, monkeypatch):
    client, window = prepare(tmp_path, qtbot, monkeypatch)
    try:
        show_work(client, window)
        clicked = dict(window.visible_segments[1])
        assert window.current_segment()['idx'] != clicked['idx']
        source = tmp_path / 'audio' / clicked['audio']
        original = source.read_bytes()
        before = client.get('/references').json()
        files_before = set((tmp_path / 'voices').glob('*'))
        refresh = lambda: window.clip_sort.setCurrentIndex(1)
        assert open_context(window, qtbot, refresh=refresh) == [True]
        dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
        assert Path(dialog.path) == source and dialog.text.toPlainText() == clicked['text']
        assert dialog.name.text() == f"生成转参考 · 片段 {clicked['idx']}"
        assert not window.clip_list.isVisible()
        dialog.reject()
        qtbot.wait(30)
        assert client.get('/references').json() == before
        assert set((tmp_path / 'voices').glob('*')) == files_before

        window.clip_sort.setCurrentIndex(0)
        window.toggle_clip_list()
        assert open_context(window, qtbot, refresh=refresh) == [True]
        dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
        dialog.name.setText('生成列表保存的声音')
        qtbot.mouseClick(dialog.confirm, Qt.MouseButton.LeftButton)
        retained = next(r for r in client.get('/references').json() if r['name'] == '生成列表保存的声音')
        assert retained['reference_text'] == clicked['text']
        copy = Path(retained['reference_audio'])
        assert copy.parent == tmp_path / 'voices' and copy != source and copy.read_bytes() == original
        assert source.read_bytes() == original
        assert any(r['id'] == retained['id'] for r in window.references)
        window.player.stop()
        window.player.setSource(QUrl())
        assert client.post('/generated-audio/delete', json={'all': True}).json()['errors'] == []
        assert not source.exists() and copy.read_bytes() == original
    finally:
        window.close()
        client.__exit__(None, None, None)


@pytest.mark.parametrize('reason', ['missing', 'failed', 'offline'])
def test_unavailable_history_disables_save(tmp_path, qtbot, monkeypatch, reason):
    client, window = prepare(tmp_path, qtbot, monkeypatch)
    try:
        show_work(client, window)
        clicked = window.visible_segments[1]
        if reason == 'missing':
            (tmp_path / 'audio' / clicked['audio']).unlink()
        elif reason == 'failed':
            clicked['status'] = 'failed'
        else:
            window.ready = False
        before = client.get('/references').json()
        assert open_context(window, qtbot, choose=False) == [False]
        assert not window.dialogs and client.get('/references').json() == before
    finally:
        window.close()
        client.__exit__(None, None, None)


def test_deleted_audio_while_menu_open_reports_error_without_import(tmp_path, qtbot, monkeypatch):
    client, window = prepare(tmp_path, qtbot, monkeypatch)
    try:
        show_work(client, window)
        clicked = window.visible_segments[1]
        errors = []
        monkeypatch.setattr(window, 'show_error', errors.append)
        assert open_context(window, qtbot, refresh=lambda: (tmp_path / 'audio' / clicked['audio']).unlink()) == [True]
        assert errors and '不存在' in errors[0]
        assert not window.dialogs and not client.get('/references').json()
    finally:
        window.close()
        client.__exit__(None, None, None)


def test_dialogue_menu_keeps_clicked_frame_when_selection_changes(environment, qtbot):
    window, client, _, first, second = environment
    window.audio_output.setMuted(True)
    window.generate()
    job = wait_job(client, window.selected_job)
    window.open_job_data(job)
    window.dialogue_editor.select(first.identity)
    clicked = dict(window.visible_segments[0])
    window.toggle_clip_list()
    QApplication.processEvents()
    assert open_context(window, qtbot, row=0, refresh=lambda: window.dialogue_editor.select(second.identity)) == [True]
    dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
    assert dialog.text.toPlainText() == clicked['text']
    assert clicked['source_id'] == first.identity
    assert Path(dialog.path).name == clicked['audio']
    dialog.reject()


def test_right_mouse_button_does_not_start_playback(tmp_path, qtbot, monkeypatch):
    client, window = prepare(tmp_path, qtbot, monkeypatch)
    try:
        show_work(client, window)
        played = []
        monkeypatch.setattr(window, 'play', lambda **_: played.append(True))
        table = window.segments_table
        table.customContextMenuRequested.disconnect(window.clip_context_menu)
        point = table.visualItemRect(table.item(1, 2)).center()
        qtbot.mouseClick(table.viewport(), Qt.MouseButton.RightButton, pos=point)
        assert not played and window.clip_list.isVisible()
    finally:
        window.close()
        client.__exit__(None, None, None)
