import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt, QPoint

from voice_workbench.api import create_app
from voice_workbench.gui import Window
from voice_workbench.desktop.components.audio_selection import AudioSelection
from test_audio_editing import wav, wait_version


def test_third_mode_keeps_article_layout_and_has_own_transport(qtbot, tmp_path):
    window = Window(18888, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.show()
    window.text_edit.setPlainText('仍然是文章内容')
    window.resize(1080, 720)
    window.start_creation('audio')
    assert window.size().width() == 1080 and window.size().height() == 720
    assert window.creation_pages.currentIndex() == 2
    assert window.text_edit.toPlainText() == '仍然是文章内容'
    assert window.editor_mode == 'article'
    assert window.transport.isHidden()
    assert set(window.creation_choices) == {'article', 'dialogue', 'audio', 'recognition', 'float'}
    window.resume_creation_editor()
    assert window.creation_pages.currentIndex() == 1 and window.transport.isVisible()
    assert window.generate_button.parent() != window.audio_editor
    window.close()


def test_waveform_drag_selects_time_range(qtbot, tmp_path):
    waveform = AudioSelection()
    qtbot.addWidget(waveform)
    waveform.resize(600, 132)
    waveform.duration = 4
    waveform.load(wav(tmp_path/'wave.wav', seconds=4))
    waveform.show()
    selected = []
    waveform.selection_changed.connect(lambda start, end: selected.append((start, end)))
    qtbot.mousePress(waveform, Qt.MouseButton.LeftButton, pos=QPoint(150, 40))
    qtbot.mouseRelease(waveform, Qt.MouseButton.LeftButton, pos=QPoint(450, 40))
    assert selected == [(1.0, 3.0)]


def test_gui_process_restores_versions_and_playback_does_not_advance(qtbot, tmp_path, monkeypatch):
    root = tmp_path/'data'
    source = wav(tmp_path/'原音频.wav')
    with TestClient(create_app(root)) as client:
        window = Window(18888, root, autostart=False)
        qtbot.addWidget(window)
        window.show()
        window.ready = True
        def call(action, success=None, failure=None):
            try:
                value = action(client)
            except Exception as exc:
                if failure:
                    failure(str(exc))
                else:
                    raise
            else:
                if success:
                    success(value)
        window.call = call
        window.open_audio_editor(source, '原文', name='来源文章')
        panel, controller = window.audio_editor, window.audio_editor.controller
        identity = controller.project['id']
        assert controller.project['state']['name'] == '来源文章'
        original_id = controller.current['id']
        controller.set_selection(0.5, 1.5)
        panel.title.setText('编辑任务')
        controller.process('render')
        panel.target_text.setPlainText('处理期间继续输入的新台词')
        wait_version(client, identity)
        controller.refresh()
        assert controller.current['id'] != original_id
        assert controller.current['details']['duration'] == 1
        assert panel.history.count() == 2
        assert panel.target_text.toPlainText() == '处理期间继续输入的新台词'
        result_id = controller.current['id']
        controller.play_range = (0, 1)
        controller.playing_version = result_id
        controller.position_changed(1000)
        assert controller.play_range is None and controller.current['id'] == result_id
        controller.undo()
        assert controller.current['id'] == original_id
        assert controller.current['details']['duration'] == 2
        assert Path(controller.current['path']).read_bytes() == source.read_bytes()
        panel.controller.stop()
        window.ready = False
        window.close()


def test_audio_draft_queue_retains_each_project_and_recovers_offline(tmp_path):
    from voice_workbench.desktop.draft_saves import DraftSaveQueue
    path = tmp_path/'pending-audio-edits.json'
    queue = DraftSaveQueue(path, title_field='name')
    older = queue.stage('one', {'name': '旧名称', 'target_text': '旧台词'})
    queue.stage('one', {'name': '新名称', 'target_text': '新台词'})
    queue.stage('two', {'name': '第二个项目', 'target_text': '另一段'})
    queue.acknowledge(older)
    recovered = DraftSaveQueue(path, title_field='name')
    assert not recovered.load_error
    assert recovered.pending['one'].body['target_text'] == '新台词'
    assert recovered.pending['two'].body['name'] == '第二个项目'
