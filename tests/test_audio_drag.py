"""任务列表里的音频可以整行拖出，交给其他程序直接使用。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from voice_workbench.api import create_app
from voice_workbench.gui import Window
from voice_workbench.recent_works import segment_audio
from voice_workbench.ui import (AudioTable, audio_drag_name, audio_mime_data, drag_cache_dir, existing_audio_files,
                                prepare_drag_files, prune_drag_cache)
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


def test_drag_name_is_the_line_plus_its_generation_time():
    stamp = time.mktime((2026, 10, 19, 18, 4, 7, 0, 0, -1))
    assert audio_drag_name("姐姐你在干什么呀？", stamp) == "姐姐你在干什么呀？_20261019-180407"
    assert audio_drag_name('a/b:c*d?e"f<g>h|i\\j', stamp) == "a b c d e f g h i j_20261019-180407"
    assert len(audio_drag_name("字" * 300, stamp)) == 60 + len("_20261019-180407")
    assert audio_drag_name("   ", stamp) == "音频_20261019-180407"


def test_prepared_copies_carry_the_audio_and_leave_the_original_alone(tmp_path):
    source = tmp_path / "0f0f0f0f.wav"
    source.write_bytes(b"RIFF" + bytes(64))
    stamp = time.time() - 3600
    os.utime(source, (stamp, stamp))
    prepared = prepare_drag_files([source], ["一段测试对白"])
    assert len(prepared) == 1 and prepared[0] != source.resolve()
    assert prepared[0].parent == drag_cache_dir().resolve()
    assert prepared[0].read_bytes() == source.read_bytes()
    assert abs(prepared[0].stat().st_mtime - stamp) < 1
    expected = "一段测试对白_" + time.strftime("%Y%m%d-%H%M%S", time.localtime(stamp)) + ".wav"
    assert prepared[0].name == expected
    assert source.is_file() and source.stat().st_size == 68


def test_a_batch_keeps_one_name_per_clip(tmp_path):
    clips = []
    for index in range(3):
        path = tmp_path / f"clip-{index}.wav"
        path.write_bytes(b"RIFF" + bytes(index))
        stamp = time.time() - 600 + index
        os.utime(path, (stamp, stamp))
        clips.append(path)
    prepared = prepare_drag_files(clips, ["第一句", "第二句"])  # 文本少于文件时回退原名
    assert len(prepared) == 3
    assert prepared[0].stem.startswith("第一句_") and prepared[1].stem.startswith("第二句_")
    assert prepared[2].stem.startswith("clip-2_")
    assert len({path.name for path in prepared}) == 3


def test_old_drag_copies_are_pruned(tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    stale, fresh = folder / "old.wav", folder / "new.wav"
    stale.write_bytes(b"RIFF")
    fresh.write_bytes(b"RIFF")
    long_ago = time.time() - 3600
    os.utime(stale, (long_ago, long_ago))
    prune_drag_cache(folder)
    assert not stale.exists() and fresh.exists()


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
    table.audio_row_texts = lambda row: ["你好，这是一句测试对白"]
    table.startDrag(Qt.DropAction.CopyAction)
    drag = FakeDrag.created[-1]
    assert drag.mime.hasUrls()
    handed = Path(drag.mime.urls()[0].toLocalFile())
    assert handed.is_file() and handed.read_bytes() == audio.read_bytes()
    assert handed.stem.startswith("你好，这是一句测试对白_")
    assert handed.suffix == ".wav"
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
    assert window.dialogue_editor.audio_root == (window.data_dir / "audio")
    # 侧栏整批拖出时，每个文件按它自己的原文命名，而不是作品名。
    FakeDrag.created.clear()
    window.jobs_table.selectRow(0)
    window.jobs_table.startDrag(Qt.DropAction.CopyAction)
    names = [Path(url.toLocalFile()).name for url in FakeDrag.created[-1].mime.urls()]
    texts = {item["text"] for item in window.visible_segments}
    assert len(names) == job["total"]
    assert all(name.endswith(".wav") and "_20" in name for name in names)
    assert all(any(name.startswith(text + "_") for text in texts) for name in names)


def test_audio_management_rows_hand_over_the_file(generated):
    window, client = generated
    window.set_page(3)
    window.settings.set_section(1)
    QApplication.processEvents()
    window.settings.refresh()
    QApplication.processEvents()
    files = window.settings.audio_table.audio_drag_files(0)
    assert files and files[0].is_file()
    texts = window.settings.audio_table.audio_row_texts(0)
    assert texts and texts[0] == window.settings.visible_audio[0]["text"]


def test_dialogue_rows_hand_over_their_latest_take(tmp_path, qtbot, monkeypatch):
    """对白框本身就是拖拽源：拖起的最新一条音频，不需要去生成记录里找。"""
    import voice_workbench.ui as ui
    from voice_workbench.desktop.components.dialogue import DialogueEditor
    monkeypatch.setattr(ui, "QDrag", FakeDrag)
    FakeDrag.created.clear()
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    older, latest = audio_dir / "take-1.wav", audio_dir / "take-2.wav"
    older.write_bytes(b"RIFF")
    latest.write_bytes(b"RIFF")
    editor = DialogueEditor()
    qtbot.addWidget(editor)
    editor.audio_root = audio_dir
    editor.load([{"id": "abc12345", "text": "姐姐你在干什么呀？", "role_name": "小雨"}])
    row = editor.rows[0]
    assert row.drag_handle.active is False and row.audio_files() == []
    assert row.drag_handle.begin_drag() is False and FakeDrag.created == []
    editor.update_records([
        {"idx": 1, "status": "done", "audio": older.name, "source_id": "abc12345",
         "source_revision": "x", "generation": 1, "generation_seconds": 1.2},
        {"idx": 2, "status": "done", "audio": latest.name, "source_id": "abc12345",
         "source_revision": "x", "generation": 2, "generation_seconds": 0.9},
    ], None, True)
    assert row.drag_handle.active
    assert row.audio_files() == [latest.resolve()]
    assert row.drag_title() == "小雨 · 姐姐你在干什么呀？"
    assert row.drag_handle.begin_drag() is True
    drag = FakeDrag.created[-1]
    assert drag.mime.hasUrls()
    handed = Path(drag.mime.urls()[0].toLocalFile())
    assert handed.read_bytes() == b"RIFF" and handed.stem.startswith("姐姐你在干什么呀？_")
    assert drag.executed == [Qt.DropAction.CopyAction]
    # 音频被删除或未生成时，手柄退回普通状态，不再发起空拖拽。
    latest.unlink()
    row.drag_handle.refresh()
    FakeDrag.created.clear()
    assert row.drag_handle.active is False
    assert row.drag_handle.begin_drag() is False and FakeDrag.created == []


def test_dialogue_row_press_without_audio_keeps_the_handle_a_click(qtbot, monkeypatch):
    import voice_workbench.ui as ui
    from voice_workbench.desktop.components.dialogue import DialogueEditor
    monkeypatch.setattr(ui, "QDrag", FakeDrag)
    FakeDrag.created.clear()
    editor = DialogueEditor()
    qtbot.addWidget(editor)
    editor.load([{"id": "abc12345", "text": "还没生成", "role_name": "小雨"}])
    row = editor.rows[0]
    assert row.drag_handle.active is False
    assert row.drag_handle.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert "生成之后" in row.drag_handle.toolTip()
    assert row.drag_handle.begin_drag() is False and FakeDrag.created == []


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
