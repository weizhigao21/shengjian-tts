"""Startup history and a stable player popover with real Qt pointer events."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
import sqlite3
import time

from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QSignalSpy

from voice_workbench.gui import Window
from voice_workbench.store import Store


def populated_window(tmp_path, qtbot):
    window = Window(8792, tmp_path, autostart=False)
    window.timer.stop()
    qtbot.addWidget(window)
    window.selected_job = "work"
    window.render_job({"id": "work", "title": "片段列表", "created": time.time(), "status": "queued", "segments": [
        {"idx": i, "text": f"片段{i}", "timestamp": "00:05.00", "created": time.time(),
         "generation": 1, "status": "queued", "audio": "", "error": None, "cached": 0}
        for i in range(1, 31)]})
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    return window


def test_name_click_reliably_toggles_and_outside_click_and_escape_close(tmp_path, qtbot):
    window = populated_window(tmp_path, qtbot)
    assert not window.clip_list.isWindow()
    for _ in range(5):
        qtbot.mouseClick(window.clip_title, Qt.MouseButton.LeftButton)
        assert window.clip_list.isVisible() and window.clip_title.isChecked()
        qtbot.mouseClick(window.clip_title, Qt.MouseButton.LeftButton)
        assert window.clip_list.isHidden() and not window.clip_title.isChecked()
    qtbot.mouseClick(window.clip_title, Qt.MouseButton.LeftButton)
    qtbot.keyClick(window.segments_table, Qt.Key.Key_Escape)
    assert window.clip_list.isHidden()
    qtbot.mouseClick(window.clip_title, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.text_edit.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
    assert window.clip_list.isHidden() and window.text_edit.hasFocus()
    window.close()


def test_poll_keeps_rows_scroll_focus_and_open_state_and_only_changes_updated_cells(tmp_path, qtbot):
    window = populated_window(tmp_path, qtbot)
    qtbot.mouseClick(window.clip_title, Qt.MouseButton.LeftButton)
    window.segments_table.selectRow(20)
    window.segments_table.verticalScrollBar().setValue(12)
    before_scroll = window.segments_table.verticalScrollBar().value()
    before = [window.segments_table.item(i, 2) for i in range(30)]
    spy = QSignalSpy(window.segments_table.model().dataChanged)
    job = deepcopy(window.job_data)
    for _ in range(4):
        window.render_job(deepcopy(job))
    assert len(spy) == 0
    assert window.clip_list.isVisible() and window.segments_table.hasFocus()
    assert window.segments_table.currentRow() == 20
    assert window.segments_table.verticalScrollBar().value() == before_scroll
    assert all(window.segments_table.item(i, 2) is before[i] for i in range(30))
    changed = deepcopy(job)
    changed["segments"][-1]["status"] = "failed"
    changed["segments"][-1]["error"] = {"message": "具体失败原因"}
    window.render_job(changed)
    assert window.clip_list.isVisible() and window.segments_table.currentRow() == 20
    assert window.segments_table.verticalScrollBar().value() == before_scroll
    assert all(window.segments_table.item(i, 2) is before[i] for i in range(30))
    assert window.segments_table.item(29, 3).text() == "失败"
    assert window.segments_table.item(29, 4).text() == "具体失败原因"
    window.close()


def test_saved_history_appears_before_any_service_request_or_create_action(tmp_path, qtbot, monkeypatch):
    store = Store(tmp_path)
    saved = store.create("启动时就显示", {}, "", [], "草稿")
    hidden = store.create("已删除的作品", {}, "", [], "")
    store.delete(hidden)
    store.close()
    calls = []
    monkeypatch.setattr(Window, "call", lambda self, *args, **kwargs: calls.append(args))
    window = Window(8792, tmp_path, autostart=True)
    window.timer.stop()
    qtbot.addWidget(window)
    assert window.jobs_table.rowCount() == 1
    assert window.jobs_table.item(0, 0).text() == "启动时就显示"
    assert window.jobs_table.item(0, 0).data(256) == saved
    assert not window.ready and len(calls) == 1
    row = window.jobs_table.item(0, 0)
    spy = QSignalSpy(window.jobs_table.model().dataChanged)
    for _ in range(3):
        window.render_jobs(Store.read_recent(tmp_path))
    assert window.jobs_table.item(0, 0) is row and len(spy) == 0
    window.close()


def test_read_recent_is_read_only_and_handles_the_legacy_schema(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY,title TEXT,voice TEXT,fingerprint TEXT,status TEXT,created REAL,updated REAL)")
        db.execute("CREATE TABLE segments (job_id TEXT,idx INTEGER,status TEXT)")
        db.execute("INSERT INTO jobs VALUES ('old','旧作品','{}','v','done',1,1)")
        db.execute("INSERT INTO segments VALUES ('old',1,'done')")
    before = path.read_bytes()
    values = Store.read_recent(tmp_path)
    assert values[0]["title"] == "旧作品" and values[0]["completed"] == 1
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert "deleted" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
