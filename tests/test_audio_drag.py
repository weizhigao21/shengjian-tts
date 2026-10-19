"""任务列表里的音频可以整行拖出，交给其他程序直接使用。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from voice_workbench.api import create_app
from voice_workbench.gui import Window
from voice_workbench.recent_works import segment_audio
from voice_workbench.ui import AudioTable, audio_mime_data, existing_audio_files
from test_service import ControlledRunner, wait_job


class FakeDrag:
    """收集拖拽参数，避免离屏测试真的进入平台拖拽循环。"""
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
def generated(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    client = TestClient(create_app(tmp_path, runner=ControlledRunner()))
    client.__enter__()
    target = client.post("/jobs", json={"title": "拖拽导出", "segments": [
        {"text": "第一句原文"}, {"text": "第二句原文"}]}).json()["job_id"]
    wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)
    window = Window(8796, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()

    def immediate(action, success=None, failure=None):
        value = action(client)
        if success:
            success(value)

    monkeypatch.setattr(window, "call", immediate)
    window.ready = True
    window.poll()
    window.selected_job = target
    window.set_page(0)
    window.resume_creation_editor()
    window.open_job_data(client.get("/jobs/" + target).json())
    window.show()
    QApplication.processEvents()
    yield window, client
    window.document_dirty = False
    window.close()
    client.__exit__(None, None, None)


def test_mime_data_carries_existing_files_only(tmp_path):
    real = tmp_path / "a.wav"
    real.write_bytes(b"RIFF")
    paths = existing_audio_files([real, real, tmp_path / "missing.wav"])
    assert paths == [real.resolve()]
    data = audio_mime_data(paths)
    assert data.hasUrls()
    assert [Path(url.toLocalFile()) for url in data.urls()] == [real.resolve()]


def test_drag_starts_with_the_row_file_and_a_preview(tmp_path, qtbot, monkeypatch):
    import voice_workbench.ui as ui
    monkeypatch.setattr(ui, "QDrag", FakeDrag)
    FakeDrag.created.clear()
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    table = AudioTable(1, 1)
    qtbot.addWidget(table)
    table.setRowCount(1)
    table.setCurrentCell(0, 0)
    table.resolve_audio = lambda row: [audio]
    table.describe_audio = lambda row: "0001 · 你好"
    table.startDrag(Qt.DropAction.CopyAction)
    drag = FakeDrag.created[-1]
    assert drag.mime.hasUrls() and Path(drag.mime.urls()[0].toLocalFile()) == audio.resolve()
    assert drag.pixmap is not None and not drag.pixmap.isNull()
    assert drag.hotspot is not None
    assert drag.executed == [Qt.DropAction.CopyAction]


def test_rows_without_a_playable_file_keep_the_press_a_click(qtbot, monkeypatch):
    import voice_workbench.ui as ui
    monkeypatch.setattr(ui, "QDrag", FakeDrag)
    FakeDrag.created.clear()
    table = AudioTable(1, 1)
    qtbot.addWidget(table)
    table.setRowCount(1)
    table.setCurrentCell(0, 0)
    table.startDrag(Qt.DropAction.CopyAction)
    assert FakeDrag.created == []


def test_segment_audio_reads_only_the_latest_finished_generation(tmp_path):
    with sqlite3.connect(tmp_path / "tasks.sqlite3") as db:
        db.execute("""CREATE TABLE segments (job_id TEXT, idx INTEGER, status TEXT, audio TEXT,
            generation INTEGER NOT NULL DEFAULT 1, audio_deleted INTEGER NOT NULL DEFAULT 0)""")
        db.executemany("INSERT INTO segments VALUES (?,?,?,?,?,?)", [
            ("job", 1, "done", "old-1.wav", 1, 0),
            ("job", 1, "done", "new-1.wav", 2, 0),
            ("job", 2, "done", "new-2.wav", 2, 0),
            ("job", 3, "done", "gone-3.wav", 2, 1),
            ("job", 4, "failed", None, 2, 0),
        ])
        db.commit()
    assert segment_audio(tmp_path, "job") == ["new-1.wav", "new-2.wav"]
    assert segment_audio(tmp_path, "job", limit=1) == ["new-1.wav"]
    assert segment_audio(tmp_path, "missing") == []
    assert segment_audio(tmp_path / "nowhere", "job") == []


def test_task_list_rows_hand_over_the_work_audio(generated, monkeypatch):
    import voice_workbench.ui as ui
    monkeypatch.setattr(ui, "QDrag", FakeDrag)
    window, client = generated
    job = client.get("/jobs").json()[0]
    assert window.segments_table.dragEnabled() and window.jobs_table.dragEnabled()
    assert window.segments_table.dragDropMode().name == "DragOnly"
    row = next(row for row, item in enumerate(window.visible_segments) if item["status"] == "done")
    window.segments_table.selectRow(row)
    files = window.segments_table.audio_drag_files(row)
    assert files and files[0].is_file() and files[0].parent == (window.data_dir / "audio").resolve()
    FakeDrag.created.clear()
    window.segments_table.startDrag(Qt.DropAction.CopyAction)
    assert FakeDrag.created[-1].mime.hasUrls()
    assert window.jobs_table.audio_drag_files(0) == [path.resolve() for path in window.job_audio_files(0)]
    assert len(window.jobs_table.audio_drag_files(0)) == job["total"]


def test_audio_management_rows_hand_over_the_file(generated):
    window, client = generated
    window.set_page(3)
    window.settings.set_section(1)
    QApplication.processEvents()
    window.settings.refresh()
    QApplication.processEvents()
    files = window.settings.audio_table.audio_drag_files(0)
    assert files and files[0].is_file()


def test_generation_list_stays_open_while_a_drag_is_running(qtbot):
    """拖拽期间目标程序会激活本窗口之外的东西，列表被隐藏会中断拖放。"""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QPushButton, QWidget
    from voice_workbench.ui import AnchoredPopover
    owner = QWidget()
    qtbot.addWidget(owner)
    anchor = QPushButton("生成记录", owner)
    popover = AnchoredPopover(owner, anchor)
    qtbot.addWidget(popover)
    owner.show()
    popover.show()
    QApplication.processEvents()
    assert popover.isVisible()
    popover.held = True
    QApplication.sendEvent(owner, QEvent(QEvent.Type.WindowDeactivate))
    assert popover.isVisible()
    popover.held = False
    QApplication.sendEvent(owner, QEvent(QEvent.Type.WindowDeactivate))
    assert not popover.isVisible()
