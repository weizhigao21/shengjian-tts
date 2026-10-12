"""Generated audio can be retained independently as a reference through its context menu."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

from fastapi.testclient import TestClient
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication, QMenu

from voice_workbench.api import create_app
from voice_workbench.gui import Window
from voice_workbench.library import ReferenceDialog
from test_service import ControlledRunner, wait_job


def prepare(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    client = TestClient(create_app(tmp_path, runner=ControlledRunner()))
    client.__enter__()
    target = client.post("/jobs", json={"title": "生成转参考", "segments": [
        {"text": "第一句原文"}, {"text": "第二句原文"}]}).json()["job_id"]
    wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)
    window = Window(8794, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()

    def immediate(action, success=None, failure=None):
        try:
            value = action(client)
            if success:
                success(value)
        except Exception as exc:
            if failure:
                failure(str(exc))
            else:
                raise

    window.call = immediate
    window.ready = True
    window.poll()
    window.set_page(3)
    window.settings.set_section(1)
    window.show()
    QApplication.processEvents()
    window.settings.refresh()
    return client, window


def open_context(settings, qtbot, select=True, refresh=None):
    checked = []

    def choose():
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        try:
            action = next(a for a in menu.actions() if a.text() == "添加为参考音频…")
            checked.append(action.isEnabled())
            if refresh:
                refresh()
            if select:
                menu.setActiveAction(action)
                qtbot.keyClick(menu, Qt.Key.Key_Return)
        finally:
            menu.close()

    table = settings.audio_table
    position = table.visualItemRect(table.item(0, 1)).center()
    QTimer.singleShot(0, choose)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position,
                             table.viewport().mapToGlobal(position))
    QApplication.sendEvent(table.viewport(), event)
    return checked


def test_context_menu_keeps_clicked_audio_cancels_cleanly_and_copies_before_deletion(tmp_path, qtbot, monkeypatch):
    client, window = prepare(tmp_path, qtbot, monkeypatch)
    try:
        settings = window.settings
        settings.audio_search.setText("第二句")
        item = dict(settings.visible_audio[0])
        source = tmp_path / "audio" / item["audio"]
        original = source.read_bytes()
        before = client.get("/references").json()
        files_before = set((tmp_path / "voices").glob("*"))
        refresh = lambda: settings.audio_search.setText("第一句")
        assert open_context(settings, qtbot, refresh=refresh) == [True]
        dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
        assert dialog.item is None and Path(dialog.path) == source
        assert dialog.text.toPlainText() == "第二句原文"
        assert dialog.name.text() == f"生成转参考 · 片段 {item['idx']}"
        dialog.reject()
        qtbot.wait(30)
        assert client.get("/references").json() == before
        assert set((tmp_path / "voices").glob("*")) == files_before and source.read_bytes() == original

        settings.audio_search.setText("第二句")
        assert open_context(settings, qtbot, refresh=refresh) == [True]
        dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
        dialog.name.setText("保存的参考声音")
        qtbot.mouseClick(dialog.confirm, Qt.MouseButton.LeftButton)
        reference = next(r for r in client.get("/references").json() if r["name"] == "保存的参考声音")
        retained = Path(reference["reference_audio"])
        assert retained.parent == tmp_path / "voices" and retained != source
        assert retained.read_bytes() == original and reference["reference_text"] == "第二句原文"
        assert source.read_bytes() == original
        assert any(r["id"] == reference["id"] for r in window.references)
        assert client.post("/generated-audio/delete", json={"all": True}).json()["errors"] == []
        assert not source.exists() and retained.read_bytes() == original
        assert next(r for r in client.get("/references").json() if r["id"] == reference["id"])["error"] is None
    finally:
        window.close()
        client.__exit__(None, None, None)


def test_missing_audio_disables_import_and_stale_action_reports_the_problem(tmp_path, qtbot, monkeypatch):
    client, window = prepare(tmp_path, qtbot, monkeypatch)
    try:
        settings = window.settings
        stale = dict(settings.visible_audio[0])
        (tmp_path / "audio" / stale["audio"]).unlink()
        settings.refresh()
        assert open_context(settings, qtbot, select=False) == [False]
        shown = []
        monkeypatch.setattr(window, "show_error", shown.append)
        before = client.get("/references").json()
        settings.add_audio_reference(stale)
        assert shown and "不存在" in shown[0] and not window.dialogs
        assert client.get("/references").json() == before
    finally:
        window.close()
        client.__exit__(None, None, None)
