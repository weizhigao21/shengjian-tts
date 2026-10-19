"""「边剪边配」悬浮窗：按行拆条、语速覆盖、任务行拖出、关窗回主窗口。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from voice_workbench.desktop.components.float_panel import estimate_seconds, format_duration
from voice_workbench.gui import Window
from voice_workbench.ui import AudioDrag


class FakeResponse:
    is_error = False
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeClient:
    """Records the request the panel would have sent, without a running service."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, **kwargs):
        self.calls.append(("post", url, json))
        return FakeResponse(self.payload)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, None))
        return FakeResponse(self.payload)


class FakeDrag:
    created = []

    def __init__(self, parent):
        self.parent, self.mime, self.pixmap, self.hotspot, self.executed = parent, None, None, None, []
        FakeDrag.created.append(self)

    def setMimeData(self, data):
        self.mime = data

    def setPixmap(self, pixmap):
        self.pixmap = pixmap

    def setHotSpot(self, point):
        self.hotspot = point

    def exec(self, action):
        self.executed.append(action)


@pytest.fixture
def window(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    window = Window(8801, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    window.ready = True
    window.show()
    window.open_float_mode(minimize=False)
    qtbot.addWidget(window.float_panel)
    QApplication.processEvents()
    yield window
    window.close()


def attach(monkeypatch, window, client):
    def immediate(action, success=None, failure=None):
        value = action(client)
        if success:
            success(value)
    monkeypatch.setattr(window, "call", immediate)


def ready_panel(window, preset=None):
    panel = window.float_panel
    panel.set_voices(preset or [{"id": "demo", "name": "演示音色", "speed": 1.0}])
    return panel


def test_estimate_and_format_read_like_a_clip_length():
    assert 0.9 < estimate_seconds(["你好世界"], 1.0) < 1.5
    assert estimate_seconds(["你好世界"], 2.0) < estimate_seconds(["你好世界"], 1.0)
    assert format_duration(9.44) == "9.4 秒"
    assert format_duration(75) == "1 分 15 秒"


def test_one_line_becomes_one_clip(window):
    panel = ready_panel(window)
    panel.per_line.setChecked(True)
    panel.editor.setPlainText("第一句\n\n  第二句  \n第三句")
    assert panel.segments() == ["第一句", "第二句", "第三句"]


def test_switching_off_keeps_the_whole_paragraph(window):
    panel = ready_panel(window)
    panel.per_line.setChecked(False)
    panel.editor.setPlainText("第一句\n第二句")
    assert panel.segments() == ["第一句\n第二句"]


def test_generation_posts_every_line_with_the_slider_speed(window, monkeypatch):
    panel = ready_panel(window)
    panel.editor.setPlainText("第一句\n第二句")
    panel.speed_slider.setValue(115)
    client = FakeClient({"job_id": "job-1", "title": "边剪边配", "segment_indices": [0, 1]})
    attach(monkeypatch, window, client)
    window.float_generate()
    method, url, body = next(call for call in client.calls if call[0] == "post")
    assert (method, url) == ("post", "/jobs")
    assert [segment["text"] for segment in body["segments"]] == ["第一句", "第二句"]
    assert body["overrides"] == {"speed": 1.15}
    assert body["use_preset"] is True
    assert body["title"].startswith("边剪边配")
    assert window.float_job_id == "job-1"
    assert panel.editor.toPlainText() == ""


def test_second_batch_appends_to_the_same_work(window, monkeypatch):
    panel = ready_panel(window)
    panel.editor.setPlainText("补一句")
    window.float_job_id = "job-9"
    client = FakeClient({"job_id": "job-9", "title": "边剪边配", "segment_indices": [4]})
    attach(monkeypatch, window, client)
    window.float_generate()
    method, url, body = next(call for call in client.calls if call[0] == "post")
    assert (method, url) == ("post", "/jobs/job-9/generate")
    assert "title" not in body


def test_generation_without_a_preset_explains_itself(window):
    panel = window.float_panel
    panel.set_voices([])
    panel.editor.setPlainText("随便一句")
    window.float_generate()
    assert "音色预设" in panel.error.text()
    assert window.float_job_id is None


def test_preset_speed_seeds_the_slider_but_the_slider_wins(window):
    panel = ready_panel(window, [{"id": "slow", "name": "慢速", "speed": 0.8},
                                 {"id": "fast", "name": "快速", "speed": 1.3}])
    assert panel.speed_slider.value() == 80
    panel.voice_combo.setCurrentIndex(1)
    assert panel.speed_slider.value() == 130
    panel.speed_slider.setValue(140)
    assert panel.speed() == 1.4


def test_rows_hand_over_the_named_audio(tmp_path, window, monkeypatch):
    import voice_workbench.ui as ui
    monkeypatch.setattr(ui, "QDrag", FakeDrag)
    FakeDrag.created.clear()
    folder = tmp_path / "audio"
    folder.mkdir()
    (folder / "a.wav").write_bytes(b"RIFF" + bytes(32))
    panel = window.float_panel
    panel.audio_root = folder
    panel.show_job({"segments": [{"idx": 0, "text": "姐姐你在干什么呀", "status": "done",
                                  "audio": "a.wav", "duration": 2.4}]})
    table = panel.table
    assert table.rowCount() == 1
    table.setCurrentCell(0, 0)
    table.startDrag(Qt.DropAction.CopyAction)
    drag = FakeDrag.created[-1]
    handed = Path(drag.mime.urls()[0].toLocalFile())
    assert handed.is_file() and handed.read_bytes() == (folder / "a.wav").read_bytes()
    assert handed.stem.startswith("姐姐你在干什么呀_")


def test_a_row_without_audio_stays_a_click(tmp_path, window, monkeypatch):
    import voice_workbench.ui as ui
    monkeypatch.setattr(ui, "QDrag", FakeDrag)
    FakeDrag.created.clear()
    panel = window.float_panel
    panel.audio_root = tmp_path / "audio"
    (tmp_path / "audio").mkdir()
    panel.show_job({"segments": [{"idx": 0, "text": "还在生成", "status": "running", "audio": None}]})
    panel.table.setCurrentCell(0, 0)
    panel.table.startDrag(Qt.DropAction.CopyAction)
    assert FakeDrag.created == []


def test_completed_rows_are_collected_for_one_drag(tmp_path, window):
    folder = tmp_path / "audio"
    folder.mkdir()
    for name in ("a.wav", "b.wav"):
        (folder / name).write_bytes(b"RIFF")
    panel = window.float_panel
    panel.audio_root = folder
    panel.show_job({"segments": [
        {"idx": 0, "text": "第一句", "status": "done", "audio": "a.wav"},
        {"idx": 1, "text": "第二句", "status": "done", "audio": "b.wav"},
        {"idx": 2, "text": "第三句", "status": "queued", "audio": None}]})
    assert len(panel.completed_files()) == 2
    assert panel.completed_texts() == ["第二句", "第一句"]


def test_new_session_empties_the_list(tmp_path, window):
    folder = tmp_path / "audio"
    folder.mkdir()
    (folder / "a.wav").write_bytes(b"RIFF")
    panel = window.float_panel
    panel.audio_root = folder
    panel.show_job({"segments": [{"idx": 0, "text": "第一句", "status": "done", "audio": "a.wav"}]})
    window.float_job_id = "job-1"
    window.float_new_session()
    assert window.float_job_id is None
    assert panel.table.rowCount() == 0
    assert panel.records == {}


def test_copy_latest_fills_the_clipboard(tmp_path, window):
    folder = tmp_path / "audio"
    folder.mkdir()
    (folder / "a.wav").write_bytes(b"RIFF")
    panel = window.float_panel
    panel.audio_root = folder
    panel.show_job({"segments": [{"idx": 0, "text": "第一句", "status": "done", "audio": "a.wav"}]})
    window.float_copy()
    urls = QApplication.clipboard().mimeData().urls()
    assert [Path(url.toLocalFile()) for url in urls] == [(folder / "a.wav").resolve()]


def test_collapsing_keeps_only_the_title_bar(window):
    panel = window.float_panel
    expanded = panel.height()
    panel.toggle()
    assert panel.collapsed and not panel.body.isVisible()
    assert panel.height() < expanded
    panel.toggle()
    assert not panel.collapsed and panel.body.isVisible()


def test_state_text_tracks_the_queue(window):
    assert Window.float_state({"segments": []}) == "待机"
    assert Window.float_state({"segments": [
        {"status": "done"}, {"status": "running"}, {"status": "queued"}]}) == "生成中 1/3"
    assert Window.float_state({"segments": [{"status": "failed"}]}) == "1 条失败"
    assert Window.float_state({"segments": [{"status": "done"}]}) == "已就绪"


def test_closing_the_panel_brings_the_main_window_back(window):
    panel = window.float_panel
    assert panel.isVisible()
    panel.close()
    assert not panel.isVisible()
    assert window.isVisible()
    assert not window.float_timer.isActive()


def test_the_panel_is_its_own_top_level_window(window):
    panel = window.float_panel
    assert panel.parent() is None
    assert panel.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert panel.windowFlags() & Qt.WindowType.FramelessWindowHint


def test_the_creation_entry_offers_the_mode(window):
    assert "float" in window.creation_choices
    assert window.creation_choices["float"].text().startswith("边剪边配")


def test_the_panel_paints_opaque_so_windows_keeps_clear_type(window):
    """An ARGB window loses subpixel antialiasing; the labels turn grey and soft."""
    panel = window.float_panel
    assert not panel.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert panel.card.graphicsEffect() is None


def test_the_first_typed_character_repaints_the_whole_editor(window):
    panel = window.float_panel
    panel.editor.clear()
    assert panel.had_text is False
    panel.editor.setPlainText("你")
    assert panel.had_text is True
    panel.editor.clear()
    assert panel.had_text is False


def test_creation_cards_wrap_three_per_row(window):
    from PyQt6.QtWidgets import QGridLayout
    from voice_workbench.desktop.layout.creation import creation_home

    keep = window.creation_choices
    try:
        page = creation_home(window)
        grid = page.findChildren(QGridLayout)[0]
        placed = {}
        for index in range(grid.count()):
            row, column, _, _ = grid.getItemPosition(index)
            placed[grid.itemAt(index).widget().text().split("\n")[0]] = (row, column)
        assert placed["文章"] == (0, 0)
        assert placed["音频编辑"] == (0, 2)
        assert placed["语音识别"] == (1, 0)
        assert placed["边剪边配"] == (1, 1)
    finally:
        window.creation_choices = keep
