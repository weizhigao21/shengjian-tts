"""Save the clicked recognition range as an independent library reference."""
from copy import deepcopy
from pathlib import Path
import wave

from fastapi.testclient import TestClient
import pytest
from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication, QLabel, QMenu

from voice_workbench.api import create_app
from voice_workbench.errors import VoiceError
from voice_workbench.gui import Window
from voice_workbench.library import ReferenceDialog
from voice_workbench.speech_recognition.reference_clip import prepare_reference_clip
from test_audio_editing import wav
from test_speech_recognition import FakeRecognition, setup_engine, wait_session


@pytest.fixture
def page(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr('voice_workbench.discovery.discover_yovoice', lambda _: [])
    monkeypatch.setattr('voice_workbench.speech_recognition.service.RecognitionRunner', FakeRecognition)
    data = tmp_path/'data'
    with TestClient(create_app(data)) as client:
        setup_engine(tmp_path, client)
        result = client.post('/recognition/sessions', json={'path': str(wav(tmp_path/'原录音.wav', seconds=8)), 'language': 'zh'})
        identity = result.json()['id']; wait_session(client, identity)
        lines = [{'index': 1, 'start': .5, 'end': 2., 'text': '第一句原文。'},
                 {'index': 2, 'start': 2., 'end': 4.5, 'text': '第二句原文。'}]
        client.app.state.manager.recognition.store.update(identity, {'lines': lines, 'text': '第一句原文。第二句原文。',
                                                                    'name': '测试录音', 'warning': '当前按标点估算时间'})
        record = client.get('/recognition/sessions/'+identity).json()
        window = Window(18992, data, autostart=False)
        qtbot.addWidget(window)
        for timer in (window.timer, window.settings.timer, window.document_timer, window.recognition.controller.timer): timer.stop()
        errors = []
        monkeypatch.setattr(window, 'show_error', errors.append)
        def call(action, success=None, failure=None):
            try:
                result = action(client)
                if success: success(result)
            except Exception as exc:
                if failure: failure(str(exc))
                else: raise
        window.call = call
        window.ready = True
        window.creation_pages.setCurrentIndex(3)
        controller = window.recognition.controller
        controller.open_session(record)
        window.reference_output.setMuted(True)
        window.show(); QApplication.processEvents()
        yield window, controller, client, errors
        for dialog in list(window.dialogs): dialog.reject()
        window.close()


def context(controller, qtbot, *, row=1, whole=False, choose=True, change=None):
    enabled = []
    def select():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        action = next(a for a in menu.actions() if a.text() == '保存到声音库…')
        enabled.append(action.isEnabled())
        if change: change()
        if choose:
            menu.setActiveAction(action); qtbot.keyClick(menu, Qt.Key.Key_Return)
        menu.close()
    widget = controller.panel.audio_name if whole else controller.panel.lines.viewport()
    position = widget.rect().center() if whole else controller.panel.lines.visualItemRect(controller.panel.lines.item(row)).center()
    QTimer.singleShot(0, select)
    QApplication.sendEvent(widget, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, widget.mapToGlobal(position)))
    return enabled


def payload(path, start=0, end=None):
    with wave.open(str(path), 'rb') as stream:
        rate = stream.getframerate(); first = round(start*rate)
        last = round(end*rate) if end is not None else stream.getnframes()
        stream.setpos(first)
        return stream.readframes(last-first)


def test_clicked_range_keeps_its_audio_and_text_through_refresh_cancel_save_and_deletion(page, qtbot):
    window, controller, client, errors = page
    original_session = deepcopy(controller.session)
    original = window.data_dir/'recognition'/controller.state()['audio']
    before = original.read_bytes()
    controller.panel.lines.setCurrentRow(0)
    def refresh():
        controller.session['state'].update(name='后来选择的录音', text='刷新后的原文', lines=[])
        controller.render_session()
    assert context(controller, qtbot, change=refresh) == [True]
    dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
    staged = Path(dialog.path)
    assert dialog.name.text() == '测试录音 · 第 2 行'
    assert dialog.text.toPlainText() == '第二句原文。'
    assert payload(staged) == payload(original, 2, 4.5)
    assert any('估算' in label.text() for label in dialog.findChildren(QLabel))
    dialog.preview_button.click()
    dialog.reject()
    qtbot.waitUntil(lambda: not staged.exists(), timeout=3000)
    assert not client.get('/references').json() and original.read_bytes() == before
    controller.open_session(original_session)
    assert context(controller, qtbot) == [True]
    dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
    staged = Path(dialog.path)
    dialog.name.setText('保留的识别片段'); dialog.confirm.click()
    reference = next(r for r in client.get('/references').json() if r['name'] == '保留的识别片段')
    retained = Path(reference['reference_audio'])
    assert reference['reference_text'] == '第二句原文。'
    assert retained.parent == window.data_dir/'voices'
    assert payload(retained) == payload(original, 2, 4.5)
    assert original.read_bytes() == before
    qtbot.waitUntil(lambda: not staged.exists(), timeout=3000)
    controller.session = None
    client.delete('/recognition/sessions/'+original_session['id'])
    assert not original.exists() and retained.is_file()
    assert not errors


def test_shift_selected_lines_save_a_contiguous_range_and_combined_text(page, qtbot):
    window, controller, _, errors = page
    for row in range(2): controller.panel.lines.item(row).setSelected(True)
    assert context(controller, qtbot) == [True]
    dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
    assert dialog.name.text() == '测试录音 · 第 1–2 行'
    assert dialog.text.toPlainText() == '第一句原文。\n第二句原文。'
    original = window.data_dir/'recognition'/controller.state()['audio']
    assert payload(Path(dialog.path)) == payload(original, .5, 4.5)
    assert not errors


def test_current_audio_name_offers_the_entire_recording(page, qtbot, monkeypatch, tmp_path):
    window, controller, _, errors = page
    assert context(controller, qtbot, whole=True) == [True]
    dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
    assert dialog.name.text() == '测试录音 · 整段录音'
    assert dialog.text.toPlainText() == controller.state()['text']
    assert payload(Path(dialog.path)) == payload(window.data_dir/'recognition'/controller.state()['audio'])
    replacement = wav(tmp_path/'另选音频.wav', seconds=3)
    monkeypatch.setattr('voice_workbench.library.QFileDialog.getOpenFileName', lambda *args: (str(replacement), ''))
    dialog.browse()
    assert dialog.path == str(replacement) and dialog.source_hint.isHidden()
    assert not errors


@pytest.mark.parametrize('reason', ['missing', 'outside', 'offline', 'busy', 'short', 'long'])
def test_unavailable_audio_or_invalid_reference_duration_disables_the_action(page, qtbot, reason):
    window, controller, client, _ = page
    if reason == 'missing':
        (window.data_dir/'recognition'/controller.state()['audio']).unlink()
    elif reason == 'outside':
        wav(window.data_dir/'outside.wav'); controller.session['state']['audio'] = '../outside.wav'
    elif reason == 'offline': window.ready = False
    elif reason == 'busy': controller.submitting = True
    elif reason == 'short': controller.state()['lines'][1]['end'] = 2.2
    elif reason == 'long': controller.state()['lines'][1]['end'] = 63
    assert context(controller, qtbot, choose=False) == [False]
    assert not window.dialogs and not client.get('/references').json()


def test_audio_deleted_while_the_menu_is_open_is_rechecked(page, qtbot):
    window, controller, client, errors = page
    source = window.data_dir/'recognition'/controller.state()['audio']
    assert context(controller, qtbot, change=source.unlink) == [True]
    assert errors and '不存在' in errors[0]
    assert not window.dialogs and not client.get('/references').json()


def test_right_click_selects_without_starting_playback(page, qtbot, monkeypatch):
    _, controller, _, _ = page
    lines = controller.panel.lines
    lines.customContextMenuRequested.disconnect()
    calls = []
    monkeypatch.setattr(controller, 'play_line', calls.append)
    point = lines.visualItemRect(lines.item(1)).center()
    qtbot.mouseClick(lines.viewport(), Qt.MouseButton.RightButton, pos=point)
    assert not calls and lines.currentRow() == 1
    qtbot.mouseClick(lines.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert calls == [1]


def test_closing_during_preparation_removes_the_temporary_clip(page):
    window, controller, client, _ = page
    calls = []
    window.call = lambda *args: calls.append(args)
    controller.save_reference(controller.reference_snapshot(1))
    assert controller.preparing_reference and len(calls) == 1
    temporary, path = calls[0][0](client)
    assert path.is_file()
    window.closing = True
    calls[0][1]((temporary, path))
    assert not path.exists() and not controller.preparing_reference and not window.dialogs
    window.closing = False


@pytest.mark.parametrize('start,end', [(0, .5), (0, 61), (-1, 2), (float('nan'), 2), (2, 1)])
def test_invalid_ranges_do_not_create_temporary_audio(tmp_path, start, end):
    with pytest.raises(VoiceError) as error:
        prepare_reference_clip(tmp_path, 'unused.wav', start, end)
    assert error.value.code == 'REFERENCE_DURATION'
