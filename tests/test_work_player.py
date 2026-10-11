"""Works retain generation history and the compact player selects that history."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
import json
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
from PyQt6.QtMultimedia import QMediaPlayer

from voice_workbench.api import create_app
from voice_workbench.manager import Manager
from voice_workbench.store import Store
from test_inline_generation import Runner, make_audio
from test_library_and_deletion import wait_job


def test_one_work_keeps_multiple_generations_snapshots_and_draft_after_restart(tmp_path):
    with TestClient(create_app(tmp_path, runner=Runner())) as client:
        draft = client.post("/jobs/draft", json={"title": "旁白"}).json()
        target = draft["id"]
        assert draft["status"] == "draft" and not draft["segments"]
        for text, speed in (("原稿", 1.0), ("修改稿", 1.3)):
            result = client.post(f"/jobs/{target}/generate", json={"title": "旁白", "script": text,
                "segments": [{"text": text, "timestamp": "00:05.00"}], "overrides": {"speed": speed}})
            assert result.status_code == 202 and result.json()["job_id"] == target
            job = wait_job(client, target)
        assert len(client.get("/jobs").json()) == 1
        assert [s["generation"] for s in job["segments"]] == [1, 2]
        assert [s["voice"]["speed"] for s in job["segments"]] == [1.0, 1.3]
        assert job["segments"][0]["audio"] != job["segments"][1]["audio"]
        assert all(s["created"] and s["batch_title"] == "旁白" for s in job["segments"])
        assert client.patch(f"/jobs/{target}/document", json={"title": "新名字", "script": "尚未生成的草稿"}).status_code == 200
        independent = client.post("/jobs", json={"segments": [{"text": "工作流批次"}]}).json()["job_id"]
        assert independent != target
        wait_job(client, independent)
    with TestClient(create_app(tmp_path, runner=Runner())) as client:
        restored = client.get(f"/jobs/{target}").json()
        assert restored["title"] == "新名字" and restored["script"] == "尚未生成的草稿"
        assert restored["segments"] == job["segments"]
        assert client.post("/jobs/missing/generate", json={"segments": [{"text": "x"}]}).status_code == 404


def test_append_while_running_keeps_running_and_new_snapshot_separate(tmp_path):
    class BlockingRunner(Runner):
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()
            self.speeds = []
        def generate(self, request, cancelled):
            self.speeds.append(request["voice"]["speed"])
            if len(self.speeds) == 1:
                self.entered.set()
                assert self.release.wait(5)
            make_audio(request["output"])
        def interrupt(self):
            self.release.set()
    runner = BlockingRunner()
    with TestClient(create_app(tmp_path, runner=runner)) as client:
        target = client.post("/jobs", json={"segments": [{"text": "same"}], "overrides": {"speed": .8}}).json()["job_id"]
        assert runner.entered.wait(5)
        assert client.post(f"/jobs/{target}/generate", json={"segments": [{"text": "same"}], "overrides": {"speed": 1.2}}).status_code == 202
        runner.release.set()
        job = wait_job(client, target)
        assert runner.speeds == [.8, 1.2]
        assert job["completed"] == 2 and len(client.get("/jobs").json()) == 1


def test_retry_uses_failed_generation_snapshot_after_later_success(tmp_path):
    class FailOnce(Runner):
        def __init__(self):
            self.calls = []
        def generate(self, request, cancelled):
            self.calls.append(request["voice"]["speed"])
            if len(self.calls) == 1:
                raise RuntimeError("CUDA out of memory")
            super().generate(request, cancelled)
    runner = FailOnce()
    with TestClient(create_app(tmp_path, runner=runner)) as client:
        target = client.post("/jobs", json={"segments": [{"text": "first"}], "overrides": {"speed": .8}}).json()["job_id"]
        assert wait_job(client, target)["status"] == "failed"
        client.post(f"/jobs/{target}/generate", json={"segments": [{"text": "second"}], "overrides": {"speed": 1.2}})
        assert wait_job(client, target)["completed"] == 1
        client.post(f"/jobs/{target}/retry")
        assert wait_job(client, target)["completed"] == 2
        assert runner.calls == [.8, 1.2, .8]


def test_legacy_snapshot_is_frozen_before_appending_new_settings(tmp_path):
    store = Store(tmp_path)
    old = {"id": "old", "speed": 1}
    target = store.create("old", old, "original", [{"text": "old"}])
    # A record written by the pre-history schema has no per-segment snapshot.
    store.db.execute("UPDATE segments SET voice=NULL,fingerprint=NULL,batch_title=NULL,created=NULL")
    store.db.commit()
    store.close()
    store = Store(tmp_path)
    try:
        store.append(target, "new", {**old, "speed": 2}, "new", [{"text": "new"}])
        assert store.get(target)["segments"][0]["voice"] == old
        assert store.get(target)["segments"][0]["fingerprint"] == "original"
    finally:
        store.close()


def test_gui_generate_reuses_work_new_document_creates_draft_and_reopens_script(tmp_path, qapp, monkeypatch):
    from voice_workbench.gui import Window
    with TestClient(create_app(tmp_path, runner=Runner())) as client:
        window = Window(8791, tmp_path, autostart=False)
        window.timer.stop()
        def call(action, success=None, failure=None):
            result = action(client)
            if success:
                success(result)
        monkeypatch.setattr(window, "call", call)
        window.poll()
        window.title_edit.setText("旁白")
        window.text_edit.setPlainText("第一稿")
        window.generate()
        target = window.selected_job
        wait_job(client, target)
        window.text_edit.setPlainText("第二稿")
        window.generate()
        job = wait_job(client, target)
        window.poll()
        assert window.selected_job == target and len(client.get("/jobs").json()) == 1
        assert job["total"] == 2
        window.open_job_data(job)
        assert window.text_edit.toPlainText() == "第二稿"
        window.new_document()
        new = window.selected_job
        assert new != target and window.job_data["status"] == "draft"
        window.text_edit.setPlainText("未生成草稿")
        window.save_document()
        assert client.get(f"/jobs/{new}").json()["script"] == "未生成草稿"
        assert window.statusBar().isHidden()
        assert window.centralWidget().layout().contentsMargins().bottom() == 0
        window.close()


def test_player_popup_navigation_pause_and_poll_preserve_selection(tmp_path, qapp, monkeypatch):
    from voice_workbench.gui import Window
    audio = tmp_path / "audio"
    audio.mkdir()
    for name in ("one.wav", "two.wav"):
        make_audio(audio / name)
    window = Window(8791, tmp_path, autostart=False)
    window.timer.stop()
    window.selected_job = "work"
    job = {"id": "work", "title": "旁白", "status": "failed", "created": time.time(), "segments": []}
    for index, (status, filename) in enumerate((("done", "one.wav"), ("failed", ""), ("queued", ""), ("done", "two.wav")), 1):
        job["segments"].append({"idx": index, "text": f"片段 {index}", "timestamp": "00:05.00", "created": time.time(),
            "generation": index, "status": status, "audio": filename, "cached": 0,
            "error": {"message": "明确的错误原因"} if status == "failed" else None})
    window.render_job(job)
    assert window.current_segment()["idx"] == 1
    assert window.play_button.isEnabled() and not window.previous_button.isEnabled() and window.next_button.isEnabled()
    calls = []
    monkeypatch.setattr(window.player, "play", lambda: calls.append("play"))
    monkeypatch.setattr(window.player, "pause", lambda: calls.append("pause"))
    window.next_button.click()
    assert window.current_segment()["idx"] == 4 and calls == ["play"]
    assert not window.next_button.isEnabled() and window.previous_button.isEnabled()
    window.render_job(deepcopy(job))
    assert window.current_segment()["idx"] == 4
    window.previous_button.click()
    assert window.current_segment()["idx"] == 1 and calls == ["play", "play"]
    monkeypatch.setattr(window.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState)
    window.toggle_playback()
    assert calls[-1] == "pause"
    window.update_play_icon(QMediaPlayer.PlaybackState.PlayingState)
    assert window.play_button.accessibleName() == "暂停"
    window.show()
    qapp.processEvents()
    window.clip_title.click()
    assert window.clip_list.isVisible() and window.segments_table.rowCount() == 4
    assert "旁白" in window.segments_table.item(0, 2).text()
    assert ":" in window.segments_table.item(0, 1).text()
    assert "字幕时间" in window.segments_table.item(0, 1).toolTip()
    assert window.retry_button.isEnabled() and not window.segments_table.isColumnHidden(4)
    window.segments_table.selectRow(3)
    window.choose_clip()
    assert window.clip_list.isHidden() and calls[-1] == "play"
    window.close()


def test_export_keeps_alternative_generation_out_of_selected_timeline(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog
    from voice_workbench.gui import Window
    (tmp_path / "audio").mkdir()
    make_audio(tmp_path / "audio" / "one.wav")
    make_audio(tmp_path / "audio" / "two.wav")
    window = Window(8791, tmp_path, autostart=False)
    window.timer.stop()
    window.selected_job = "work"
    job = {"id": "work", "title": "作品", "voice": {}, "status": "done", "segments": [
        {"idx": i, "generation": i, "created": time.time(), "text": f"版本{i}", "timestamp": "00:00.00",
         "status": "done", "cached": 0, "error": None, "audio": name, "voice": {"speed": i}}
        for i, name in ((1, "one.wav"), (2, "two.wav"))]}
    window.render_job(job)
    window.segments_table.selectRow(1)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path / "export"))
    window.export()
    folder = tmp_path / "export" / "work" / "生成-002"
    manifest = json.loads((folder / "segments.json").read_text(encoding="utf-8"))
    assert len(list(folder.glob("*.wav"))) == 1
    assert [entry["text"] for entry in manifest] == ["版本2"]
    assert manifest[0]["voice"] == {"speed": 2} and manifest[0]["generation"] == 2
    window.close()


def test_pending_autosave_finishes_before_generation_and_pending_submit_is_not_duplicated(tmp_path, qapp, monkeypatch):
    from voice_workbench.gui import Window
    with TestClient(create_app(tmp_path, runner=Runner())) as client:
        window = Window(8791, tmp_path, autostart=False)
        window.timer.stop()
        def immediate(action, success=None, failure=None):
            result = action(client)
            if success:
                success(result)
        monkeypatch.setattr(window, "call", immediate)
        window.poll()
        window.new_document()
        target = window.selected_job
        pending = []
        monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: pending.append((action, success)))
        window.text_edit.setPlainText("早先的草稿")
        window.save_document()
        assert window.document_save_running
        window.text_edit.setPlainText("用于生成的新稿")
        window.generate()
        window.generate()
        assert window.submitting and len(pending) == 1
        action, success = pending.pop(0)
        success(action(client))
        assert len(pending) == 1
        action, success = pending.pop(0)
        result = action(client)
        monkeypatch.setattr(window, "call", immediate)
        success(result)
        job = wait_job(client, target)
        assert job["script"] == "用于生成的新稿" and job["total"] == 1
        window.close()
