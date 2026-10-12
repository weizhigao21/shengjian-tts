"""Real Qt completion, stable mention identities and multi-voice inference."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QTextCursor, QInputMethodEvent

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.desktop.components.voice_mentions import PresetScriptEditor, VOICE_ROLE
from voice_workbench.gui import Window
from voice_workbench.services.script_voices import parse_script, utf16_length
from voice_workbench.store import Store
from test_inline_generation import Runner, index_voice
from test_library_and_deletion import wait_job


def presets():
    return [{"id": "older", "name": "温柔 御姐", "backend": "windows_sapi"},
            {"id": "young", "name": "萝莉", "backend": "windows_sapi"},
            {"id": "template", "name": "默认模型设置", "backend": "windows_sapi", "is_template": True}]


def editor(qtbot):
    widget = PresetScriptEditor()
    qtbot.addWidget(widget)
    widget.resize(650, 300)
    widget.set_voices(presets())
    widget.show()
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus)
    return widget


def type_at(qtbot, widget):
    qtbot.keyClicks(widget, "@")
    qtbot.waitUntil(widget.completer.popup().isVisible)
    return widget.completer.popup()


def test_at_opens_all_presets_and_enter_inserts_name_space_and_identity(qtbot):
    widget = editor(qtbot)
    popup = type_at(qtbot, widget)
    assert widget.completer.completionCount() == 2
    qtbot.keyClick(widget, Qt.Key.Key_Return)
    assert widget.toPlainText() == "@温柔 御姐 "
    assert popup.isHidden()
    widget.insertPlainText("你好")
    assert widget.segments() == [{"text": "你好", "timestamp": "", "voice_id": "older"}]
    assert widget.mentions() == [{"start": 0, "end": 6, "preset_id": "older", "label": "温柔 御姐"}]


def test_mouse_selection_uses_clicked_id_and_does_not_insert_newline(qtbot):
    widget = editor(qtbot)
    popup = type_at(qtbot, widget)
    index = widget.completer.completionModel().index(1, 0)
    qtbot.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=popup.visualRect(index).center())
    assert widget.toPlainText() == "@萝莉 "
    assert widget.mentions()[0]["preset_id"] == "young"
    assert popup.isHidden()


def test_ime_filters_presets_and_escape_is_stable(qtbot):
    widget = editor(qtbot)
    popup = type_at(qtbot, widget)
    event = QInputMethodEvent()
    event.setCommitString("萝")
    widget.inputMethodEvent(event)
    assert widget.completer.completionCount() == 1
    assert widget.completer.completionModel().index(0, 0).data(VOICE_ROLE)["id"] == "young"
    qtbot.keyClick(widget, Qt.Key.Key_Escape)
    assert popup.isHidden() and widget.toPlainText() == "@萝"
    widget.refresh_completion()
    assert popup.isHidden()
    qtbot.keyClick(widget, Qt.Key.Key_Backspace)
    qtbot.waitUntil(popup.isVisible)


def test_mid_sentence_at_email_and_programmatic_loading_do_not_open_popup(qtbot):
    widget = editor(qtbot)
    qtbot.keyClicks(widget, "user@example.com")
    assert widget.completer.popup().isHidden()
    widget.setPlainText("@温柔 御姐 保存的对白")
    assert widget.completer.popup().isHidden()
    assert widget.segments()[0]["voice_id"] == "older"


def test_saved_identity_survives_preset_rename_and_deletion_is_explicit(qtbot):
    widget = editor(qtbot)
    type_at(qtbot, widget)
    qtbot.keyClick(widget, Qt.Key.Key_Return)
    widget.insertPlainText("你好")
    text, mentions = widget.toPlainText(), widget.mentions()
    renamed = [{**v, "name": "改名后的御姐"} if v["id"] == "older" else v for v in presets()]
    widget.setPlainText(text)
    widget.restore_mentions(mentions)
    widget.set_voices(renamed)
    assert widget.segments()[0]["voice_id"] == "older"
    widget.set_voices([v for v in renamed if v["id"] != "older"])
    with pytest.raises(ValueError, match="已删除"):
        widget.segments()


def test_editing_or_deleting_a_mention_never_leaves_a_hidden_voice(qtbot):
    widget = editor(qtbot)
    type_at(qtbot, widget)
    qtbot.keyClick(widget, Qt.Key.Key_Return)
    widget.insertPlainText("正文")
    cursor = widget.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(7, QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    widget.setTextCursor(cursor)
    assert widget.mentions() == []
    assert widget.segments() == [{"text": "正文", "timestamp": ""}]
    widget.undo()
    assert widget.mentions()[0]["preset_id"] == "older"


def test_utf16_offsets_after_emoji_and_duplicate_names_remain_unambiguous(qtbot):
    widget = editor(qtbot)
    widget.insertPlainText("😀第一行\n")
    type_at(qtbot, widget)
    qtbot.keyClick(widget, Qt.Key.Key_Return)
    widget.insertPlainText("第二行")
    mention = widget.mentions()[0]
    assert mention["start"] == utf16_length("😀第一行\n")
    text = widget.toPlainText()
    same_name = {"id": "duplicate", "name": "温柔 御姐", "backend": "windows_sapi"}
    widget.set_voices(presets() + [same_name])
    assert widget.segments()[1]["voice_id"] == "older"
    with pytest.raises(ValueError, match="同名"):
        parse_script(text, presets() + [same_name])
    widget.setPlainText(text)
    widget.restore_mentions([mention])
    assert widget.segments()[1]["voice_id"] == "older"


@pytest.mark.parametrize("text,match", [("@不存在 正文", "没有匹配"), ("@萝莉", "输入要生成的正文"),
                                       ("@萝莉，正文", "空格分隔")])
def test_invalid_mentions_fail_before_submission(text, match):
    with pytest.raises(ValueError, match=match):
        parse_script(text, presets())


def test_plain_subtitles_literal_at_and_longest_names_keep_their_meaning():
    srt = "1\n00:00:01,000 --> 00:00:02,000\nhello@example.com\n"
    assert parse_script(srt, presets()) == [{"text": "hello@example.com", "timestamp": "00:00:01,000"}]
    with pytest.raises(ValueError, match="带时间轴"):
        parse_script(srt.replace("hello@example.com", "@萝莉 你好"), presets())
    assert parse_script("@@字面文字", presets())[0]["text"] == "@字面文字"
    voices = presets() + [{"id": "short", "name": "温柔", "backend": "windows_sapi"}]
    assert parse_script("@温柔 御姐 你好\n默认音色", voices) == [
        {"text": "你好", "timestamp": "", "voice_id": "older"}, {"text": "默认音色", "timestamp": ""}]


def test_mixed_presets_create_one_batch_with_separate_snapshots_and_default_overrides(tmp_path):
    class Capture(Runner):
        def __init__(self): self.requests = []
        def generate(self, request, cancelled):
            self.requests.append(deepcopy(request))
            super().generate(request, cancelled)
    voice = index_voice(tmp_path)
    root = tmp_path / "data"
    config = ConfigStore(root)
    first = config.save_voice({**voice, "id": "older", "name": "温柔 御姐", "speed": 1.1})
    second = config.save_voice({**voice, "id": "young", "name": "萝莉", "speed": 1.3})
    runner = Capture()
    script = "@温柔 御姐 你好\n@萝莉 欢迎\n默认音色"
    mentions = [{"start": 0, "end": 6, "preset_id": "older", "label": "温柔 御姐"}]
    with TestClient(create_app(root, runner=runner)) as client:
        body = {"voice_id": first["id"], "script": script, "script_mentions": mentions,
                "segments": parse_script(script, config.voices()), "overrides": {"speed": 1.8}, "title": "多音色"}
        response = client.post("/jobs", json=body)
        assert response.status_code == 202, response.text
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "done" and len(client.get("/jobs").json()) == 1
        assert [r["voice"]["speed"] for r in runner.requests] == [1.1, 1.3, 1.8]
        assert [r["text"] for r in runner.requests] == ["你好", "欢迎", "默认音色"]
        assert [s["provenance"]["preset_id"] for s in job["segments"]] == ["older", "young", "older"]
        assert all(s["generation"] == 1 for s in job["segments"])
        assert job["script_mentions"] == mentions
        append = client.post(f"/jobs/{job['id']}/generate", json=body)
        assert append.status_code == 202 and append.json()["generation"] == 2
        wait_job(client, job["id"])
        assert len(client.get("/jobs").json()) == 1
        original = config.voices()
        assert next(v for v in original if v["id"] == "older")["speed"] == 1.1
        failed = client.post("/jobs", json={**body, "segments": [{"text": "错误", "voice_id": "removed"}]})
        assert failed.status_code == 404 and len(client.get("/jobs").json()) == 1


def test_draft_and_reopen_restore_mentions_and_clear_them_on_plain_document_update(tmp_path, qtbot, monkeypatch):
    root = tmp_path / "data"
    voice = index_voice(tmp_path)
    config = ConfigStore(root)
    config.save_voice({**voice, "id": "older", "name": "温柔 御姐"})
    text = "@温柔 御姐 保存这句话"
    mentions = [{"start": 0, "end": 6, "preset_id": "older", "label": "温柔 御姐"}]
    with TestClient(create_app(root, runner=Runner())) as client:
        saved = client.post("/jobs/draft", json={"script": text, "script_mentions": mentions}).json()
        window = Window(8799, root, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        window.settings.timer.stop()
        window.render((client.get("/health").json(), client.get("/voices").json(), client.get("/jobs").json(), None))
        window.selected_job = saved["id"]
        window.open_job_data(saved)
        assert window.text_edit.mentions() == mentions
        assert window.text_edit.segments()[0]["voice_id"] == "older"
        assert window.generate_button.isEnabled()
        client.patch(f"/jobs/{saved['id']}/document", json={"script": "普通文本"})
        assert client.get(f"/jobs/{saved['id']}").json()["script_mentions"] == []
        window.document_dirty = False
        window.close()


def test_old_database_is_backed_up_before_additive_migration(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,voice TEXT,fingerprint TEXT,status TEXT,created REAL,updated REAL)")
        db.execute("INSERT INTO jobs VALUES('old','旧作品','{}','','draft',1,1)")
    store = Store(tmp_path)
    assert store.get("old")["script_mentions"] == []
    backups = list((tmp_path / "backups").glob("before-voice-mentions-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert "script_mentions" not in {r[1] for r in backup.execute("PRAGMA table_info(jobs)")}
        assert backup.execute("SELECT title FROM jobs").fetchone()[0] == "旧作品"
    store.close()
    store = Store(tmp_path)
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    store.close()
