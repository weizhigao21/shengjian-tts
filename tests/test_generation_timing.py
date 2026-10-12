"""Elapsed inference time survives API/storage and is distinct from audio length."""
import json
import os
import sqlite3
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.gui import Window
from voice_workbench.store import Store
from test_service import ControlledRunner, wait_job


class TimedRunner(ControlledRunner):
    def __init__(self):
        super().__init__()
        self.clock = 8000.0
        self.elapsed = 4.25

    def generate(self, request, cancelled):
        self.clock += self.elapsed
        super().generate(request, cancelled)


def test_api_times_each_attempt_and_cache_without_queue_or_audio_length(tmp_path, monkeypatch):
    runner = TimedRunner()
    monkeypatch.setattr("voice_workbench.manager.perf_counter", lambda: runner.clock)
    with TestClient(create_app(tmp_path, runner)) as client:
        get = lambda key: client.get(f"/jobs/{key}").json()
        def submit(text):
            return client.post("/jobs", json={"title": "耗时测试", "segments": [{"text": text}]}).json()["job_id"]
        target = submit("首次生成")
        first = wait_job(get, target)["segments"][0]
        assert first["generation_seconds"] == 4.25 and first["duration"] == 1.0
        runner.clock += 90000  # Time between requests never contributes to inference.
        repeated = wait_job(get, submit("首次生成"))["segments"][0]
        assert repeated["cached"] and repeated["generation_seconds"] == 0.0
        assert runner.calls == ["首次生成"]
        runner.fail_once.add("重试")
        runner.elapsed = 2.5
        retry_id = submit("重试")
        failed = wait_job(get, retry_id)["segments"][0]
        assert failed["generation_seconds"] == 2.5 and failed["status"] == "failed"
        runner.elapsed = 65.75
        assert client.post(f"/jobs/{retry_id}/retry").status_code == 200
        retried = wait_job(get, retry_id)["segments"][0]
        assert retried["generation_seconds"] == 65.75 and not retried["cached"]
        rows = client.get("/generated-audio").json()
        assert next(a for a in rows if a["job_id"] == retry_id)["generation_seconds"] == 65.75
    reopened = Store(tmp_path)
    try:
        assert reopened.get(target)["segments"][0]["generation_seconds"] == 4.25
        assert reopened.get(retry_id)["segments"][0]["generation_seconds"] == 65.75
    finally:
        reopened.close()


def test_old_database_migration_preserves_audio_and_does_not_invent_timing(tmp_path):
    voice, fingerprint = ConfigStore(tmp_path).resolve("default")
    with sqlite3.connect(tmp_path / "tasks.sqlite3") as db:
        db.executescript("""CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,voice TEXT,fingerprint TEXT,status TEXT,created REAL,updated REAL);
            CREATE TABLE segments(job_id TEXT,idx INTEGER,text TEXT,timestamp TEXT,status TEXT,
                audio TEXT,error TEXT,attempts INTEGER DEFAULT 0,cached INTEGER DEFAULT 0,PRIMARY KEY(job_id,idx));""")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)", ("old", "旧作品", json.dumps(voice), fingerprint, "done", 1234.5, 1234.5))
        db.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)", ("old", 1, "旧音频原文", "00:01.00", "done", "old.wav", None, 1, 0))
    store = Store(tmp_path)
    try:
        item = store.get("old")["segments"][0]
        assert item["generation_seconds"] is None
        assert (item["text"], item["timestamp"], item["audio"], item["created"]) == ("旧音频原文", "00:01.00", "old.wav", 1234.5)
        assert store.generated_audio()[0]["generation_seconds"] is None
    finally:
        store.close()


def test_both_audio_lists_show_elapsed_cache_and_old_records_without_refresh_flicker(tmp_path, qtbot, monkeypatch):
    runner = TimedRunner()
    monkeypatch.setattr("voice_workbench.manager.perf_counter", lambda: runner.clock)
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    with TestClient(create_app(tmp_path, runner)) as client:
        target = client.post("/jobs", json={"title": "音频耗时", "segments": [
            {"text": "第一句"}, {"text": "第一句"}, {"text": "旧记录"}]}).json()["job_id"]
        job = wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)
        # Simulate a pre-upgrade successful segment in a mixed work.
        with client.app.state.manager.store.lock, client.app.state.manager.store.db:
            client.app.state.manager.store.db.execute("UPDATE segments SET generation_seconds=NULL WHERE job_id=? AND idx=3", (target,))
        job = client.get(f"/jobs/{target}").json()
        audios = client.get("/generated-audio").json()
        window = Window(8794, tmp_path, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        window.settings.timer.stop()
        window.selected_job = target
        window.render_job(job)
        table = window.segments_table
        assert table.horizontalHeaderItem(3).text() == "生成耗时"
        assert [table.item(r, 3).text() for r in range(3)] == ["4.2 秒", "缓存", "未记录"]
        assert "不包含排队" in table.item(0, 3).toolTip()
        assert table.isColumnHidden(5) and table.item(0, 4).text() == "已完成"
        table.selectRow(1)
        timing = table.item(0, 3)
        window.render_job(job)
        assert table.item(0, 3) is timing and table.currentRow() == 1
        settings = window.settings
        settings.audio_data = audios
        settings.render_audio()
        assert settings.audio_table.horizontalHeaderItem(5).text() == "生成耗时"
        assert {a["idx"]: settings.audio_table.item(r, 5).text() for r, a in enumerate(settings.visible_audio)} == {1: "4.2 秒", 2: "缓存", 3: "未记录"}
        settings.audio_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        timing = settings.audio_table.item(0, 5)
        play = settings.audio_table.cellWidget(0, 6)
        settings.render_audio()
        assert settings.audio_table.item(0, 5) is timing
        assert settings.audio_table.cellWidget(0, 6) is play
        assert len(settings.selected_ids) == 1
        shown = []
        monkeypatch.setattr(window, "show_details", shown.append)
        settings.audio_details(next(a for a in audios if a["idx"] == 1))
        assert "生成耗时：4.2 秒" in shown[0]
        window.close()
