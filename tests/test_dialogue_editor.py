"""Real dialogue editing, persistence and independent generation histories."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.desktop.components.dialogue import DialogueEditor
from voice_workbench.gui import Window
from voice_workbench.store import Store
from test_inline_generation import Runner, index_voice
from test_library_and_deletion import wait_job
from test_voice_mentions import presets, type_at


def panel(qtbot):
    widget = DialogueEditor()
    qtbot.addWidget(widget)
    widget.resize(720, 500)
    widget.set_voices(presets())
    widget.show()
    return widget


def test_dialogue_at_binds_voice_icon_and_mouse_hover_tracks_choice(qtbot):
    widget = panel(qtbot)
    row = widget.rows[0]
    row.text.setFocus()
    qtbot.waitUntil(row.text.hasFocus)
    popup = type_at(qtbot, row.text)
    second = row.text.completer.completionModel().index(1, 0)
    qtbot.mouseMove(popup.viewport(), popup.visualRect(second).center())
    qtbot.waitUntil(lambda: popup.currentIndex().row() == 1)
    assert "萝莉" in second.data(Qt.ItemDataRole.ToolTipRole)
    qtbot.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=popup.visualRect(second).center())
    assert row.voice_id == "young" and not row.voice_button.icon().isNull()
    assert row.text.toPlainText() == "" and popup.isHidden()
    row.text.insertPlainText("你好呀！")
    assert widget.segments()[0]["text"] == "你好呀！"
    assert widget.segments()[0]["voice_id"] == "young"
    qtbot.keyClick(row.text, Qt.Key.Key_Return)
    assert len(widget.rows) == 2 and len(widget.segments()) == 1
    widget.rows[1].text.insertPlainText("第一行")
    qtbot.keyClick(widget.rows[1].text, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    widget.rows[1].text.insertPlainText("第二行")
    assert len(widget.rows) == 2 and widget.rows[1].text.toPlainText() == "第一行\n第二行"


def test_text_grows_without_clipping_and_invalid_tags_or_deleted_presets_are_explicit(qtbot):
    widget = panel(qtbot)
    row = widget.rows[0]
    initial = row.text.height()
    row.text.setPlainText("一段完整的长对白。" * 240)
    qtbot.waitUntil(lambda: row.text.height() > initial)
    assert row.text.toPlainText().endswith("长对白。")
    row.text.setPlainText("@不存在 正文")
    with pytest.raises(ValueError, match="选择音色"):
        widget.segments()
    row.text.setPlainText("@@字面符号")
    assert widget.segments()[0]["text"] == "@字面符号"
    row.set_voice(presets()[0])
    widget.set_voices(presets()[1:])
    with pytest.raises(ValueError, match="已删除"):
        widget.segments()


def test_mode_entry_and_independent_buffers_save_and_reopen(tmp_path, qtbot, monkeypatch):
    with TestClient(create_app(tmp_path, runner=Runner())) as client:
        window = Window(8791, tmp_path, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: success(action(client)) if success else action(client))
        window.poll()
        window.show()
        window.navigate(0)
        assert window.creation_pages.currentIndex() == 0
        window.new_document()
        target = window.selected_job
        assert window.creation_pages.currentIndex() == 0
        window.creation_choices["article"].click()
        window.text_edit.setPlainText("文章保留原样")
        window.mode_buttons["dialogue"].click()
        row = window.dialogue_editor.rows[0]
        identity = row.identity
        row.text.setPlainText("你好，对话")
        row.set_voice(next(v for v in window.voices if not v.get("is_template")))
        window.mode_buttons["article"].click()
        assert window.text_edit.toPlainText() == "文章保留原样"
        window.mode_buttons["dialogue"].click()
        window.save_document()
        saved = client.get(f"/jobs/{target}").json()
        assert saved["editor_state"]["mode"] == "dialogue"
        assert saved["editor_state"]["article_text"] == "文章保留原样"
        assert saved["editor_state"]["dialogue_entries"][0]["id"] == identity
        window.dialogue_editor.load([])
        window.open_job_data(saved)
        assert window.editor_mode == "dialogue"
        assert window.dialogue_editor.rows[0].identity == identity
        assert window.dialogue_editor.rows[0].text.toPlainText() == "你好，对话"
        assert window.dialogue_editor.rows[0].voice_id == row.voice_id
        assert window.text_edit.height() > 200
        window.close()


def test_mixed_dialogue_generates_in_one_work_and_force_really_infers_without_overwriting(tmp_path, qtbot):
    class RecordingRunner(Runner):
        def __init__(self):
            self.calls = []
        def generate(self, request, cancelled):
            self.calls.append(request["voice"]["id"])
            super().generate(request, cancelled)
    runner = RecordingRunner()
    voice = index_voice(tmp_path)
    data = tmp_path / "data"
    config = ConfigStore(data)
    for identity, name, speed in (("older", "御姐", .9), ("young", "萝莉", 1.2)):
        config.save_voice({**voice, "id": identity, "name": name, "speed": speed})
    widget = panel(qtbot)
    with TestClient(create_app(data, runner=runner)) as client:
        voices = client.get("/voices").json()
        widget.set_voices(voices)
        row = widget.rows[0]
        row.set_voice(next(v for v in voices if v["id"] == "older"))
        row.text.setPlainText("你好")
        second = widget.add_after(row)
        second.set_voice(next(v for v in voices if v["id"] == "young"))
        second.text.setPlainText("你好")
        draft = client.post("/jobs/draft", json={"editor_state": {"mode": "dialogue", "dialogue_entries": widget.entries()}}).json()
        target = draft["id"]
        result = client.post(f"/jobs/{target}/generate", json={"segments": widget.segments()})
        assert result.status_code == 202
        job = wait_job(client, target)
        assert runner.calls == ["older", "young"]
        assert len(client.get("/jobs").json()) == 1
        assert [s["source_id"] for s in job["segments"]] == [row.identity, second.identity]
        assert job["editor_state"]["mode"] == "dialogue"
        widget.update_records(job["segments"], None, True)
        assert "已生成" in row.status.text()
        original = data / "audio" / job["segments"][0]["audio"]
        contents = original.read_bytes()
        row.text.setPlainText("修改正文")
        widget.update_records(job["segments"], None, True)
        assert row.status.text() == "已修改" and row.play_button.isEnabled()
        row.text.setPlainText("你好")
        result = client.post(f"/jobs/{target}/generate", json={"segments": widget.segments(identity=row.identity, force=True)})
        assert result.status_code == 202
        job = wait_job(client, target)
        assert runner.calls == ["older", "young", "older"]
        assert job["segments"][-1]["cached"] == 0
        assert job["segments"][-1]["audio"] != original.name
        assert original.read_bytes() == contents
        widget.remove_row(second)
        widget.update_records(job["segments"], None, True)
        assert row.latest["idx"] == 3


def test_existing_database_is_backed_up_before_dialogue_migration(tmp_path):
    store = Store(tmp_path)
    target = store.create("旧文章", {"id": "old"}, "fingerprint", [{"text": "原文"}], script="原文")
    store.db.execute("ALTER TABLE jobs DROP COLUMN editor_state")
    for column in ("source_id", "source_revision", "force_generate"):
        store.db.execute(f"ALTER TABLE segments DROP COLUMN {column}")
    store.db.commit()
    old = store.get(target)
    store.close()
    restored = Store(tmp_path)
    assert restored.get(target)["script"] == "原文"
    assert restored.get(target)["segments"][0]["audio"] == old["segments"][0]["audio"]
    backups = list((tmp_path / "backups").glob("before-dialogue-editor-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT script FROM jobs WHERE id=?", (target,)).fetchone()[0] == "原文"
        assert "editor_state" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    restored.close()
