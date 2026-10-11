import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sqlite3
import threading
import time

from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.manager import Manager
from voice_workbench.store import Store
from test_inline_generation import index_voice, make_audio, Runner


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("status") in ("done", "failed", "cancelled"):
            return job
        time.sleep(.01)
    raise AssertionError("Task did not settle")


def test_delete_restore_preserves_shared_audio_and_does_not_reappear_after_restart(tmp_path):
    data = tmp_path / "data"
    with TestClient(create_app(data, runner=Runner())) as client:
        body = {"voice_id": "default", "segments": [{"text": "shared output"}]}
        first = client.post("/jobs", json=body).json()["job_id"]
        original = wait_job(client, first)
        second = client.post("/jobs", json=body).json()["job_id"]
        other = wait_job(client, second)
        assert other["segments"][0]["cached"] and original["segments"][0]["audio"] == other["segments"][0]["audio"]
        assert client.delete(f"/jobs/{first}").status_code == 200
        assert client.get(f"/jobs/{first}").status_code == 404
        assert first not in [j["id"] for j in client.get("/jobs").json()]
        assert client.get(f"/jobs/{second}").json()["status"] == "done"
        assert (data / "audio" / original["segments"][0]["audio"]).is_file()
    with TestClient(create_app(data, runner=Runner())) as client:
        assert first not in [j["id"] for j in client.get("/jobs").json()]
        restored = client.post(f"/jobs/{first}/restore")
        assert restored.status_code == 200 and restored.json()["status"] == "done"
        renamed = client.patch(f"/jobs/{first}", json={"title": "新的任务名称"})
        assert renamed.json()["title"] == "新的任务名称"
        assert client.patch(f"/jobs/{first}", json={"title": "   "}).status_code == 400


def test_deleting_running_task_interrupts_without_late_result_resurrection(tmp_path):
    class BlockingRunner(Runner):
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()
        def generate(self, request, cancelled):
            self.entered.set()
            assert self.release.wait(5)
            make_audio(request["output"])
        def interrupt(self):
            self.release.set()
    runner = BlockingRunner()
    manager = Manager(tmp_path, runner)
    try:
        job_id = manager.submit("running", "default", [{"text": "waiting"}])
        assert runner.entered.wait(5)
        manager.delete(job_id)
        deadline = time.monotonic() + 5
        while manager.current and time.monotonic() < deadline:
            time.sleep(.01)
        assert manager.current is None
        assert manager.store.get(job_id) is None and not manager.store.list()
        manager.restore(job_id)
        restored = manager.store.get(job_id)
        assert restored["status"] == "cancelled" and not restored["segments"][0]["audio"]
    finally:
        manager.close()


def test_task_schema_migration_keeps_old_records(tmp_path):
    db = sqlite3.connect(tmp_path / "tasks.sqlite3")
    db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, voice TEXT, fingerprint TEXT,status TEXT,created REAL,updated REAL)")
    db.execute("INSERT INTO jobs VALUES ('old','old task','{}','v','done',1,1)")
    db.commit()
    db.close()
    store = Store(tmp_path)
    try:
        assert store.get("old")["title"] == "old task"
        store.delete("old")
        assert not store.list()
        store.restore("old")
        assert store.get("old")["title"] == "old task"
    finally:
        store.close()


def test_reference_library_is_independent_of_models_and_keeps_deleted_references_hidden(tmp_path):
    data = tmp_path / "data"
    source = tmp_path / "reference.wav"
    make_audio(source)
    with TestClient(create_app(data, runner=Runner())) as client:
        response = client.post("/references", json={"name": "温柔女声", "reference_audio": str(source), "reference_text": "参考原文"})
        assert response.status_code == 201
        reference = response.json()
        assert Path(reference["reference_audio"]).parent == data / "voices"
        assert reference["duration"] == 2
        source.unlink()
        assert next(r for r in client.get("/references").json() if r["id"] == reference["id"])["error"] is None
        assert client.patch(f"/references/{reference['id']}", json={"name": "旁白", "reference_text": "新的原文"}).status_code == 200
        voice = index_voice(tmp_path)
        voice["reference_audio"] = reference["reference_audio"]
        assert client.put(f"/voices/{voice['id']}", json=voice).status_code == 200
        assert client.delete(f"/references/{reference['id']}").status_code == 200
        ConfigStore(data).sync_references()
        assert reference["id"] not in [r["id"] for r in client.get("/references").json()]
        assert Path(reference["reference_audio"]).is_file()
        assert client.post(f"/references/{reference['id']}/restore").status_code == 200
        restored = next(r for r in client.get("/references").json() if r["id"] == reference["id"])
        assert restored["name"] == "旁白" and restored["reference_text"] == "新的原文"


def test_deleting_last_preset_keeps_model_available_and_restore_keeps_old_identity(tmp_path):
    voice = index_voice(tmp_path)
    store = ConfigStore(tmp_path / "data")
    store.save_voice(voice)
    store.delete_voice(voice["id"])
    templates = [v for v in store.voices() if v.get("is_template") and v["model_dir"] == voice["model_dir"]]
    assert len(templates) == 1 and not templates[0]["reference_audio"]
    store.restore_voice(voice["id"])
    assert not any(v.get("is_template") and v["model_dir"] == voice["model_dir"] for v in store.voices())
    assert next(v for v in store.voices() if v["id"] == voice["id"])["name"] == voice["name"]


def test_context_menu_deletes_target_row_and_undo_restores_it(tmp_path, qapp, monkeypatch):
    from voice_workbench.gui import Window
    data = tmp_path / "data"
    with TestClient(create_app(data, runner=Runner())) as client:
        jobs = []
        for title in ("保留这个任务", "删除这个任务"):
            job_id = client.post("/jobs", json={"title": title, "segments": [{"text": title}]}).json()["job_id"]
            wait_job(client, job_id)
            jobs.append(job_id)
        window = Window(8789, data, autostart=False)
        window.timer.stop()
        def call(action, success=None, failure=None):
            value = action(client)
            if success:
                success(value)
        monkeypatch.setattr(window, "call", call)
        window.poll()
        target = jobs[1]
        row = next(i for i in range(window.jobs_table.rowCount()) if window.jobs_table.item(i, 0).data(256) == target)
        window.jobs_table.selectRow(row)
        assert window.selected_job == target
        menu = window.job_menu(row)
        window.set_page(1)
        menu.actions()[0].trigger()
        assert window.tabs.currentIndex() == 0 and window.selected_job == target
        action = next(a for a in menu.actions() if a.objectName() == "deleteJob")
        assert window.jobs_table.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
        action.trigger()
        assert window.selected_job is None and not window.job_data
        assert target not in [j["id"] for j in client.get("/jobs").json()]
        assert jobs[0] in [j["id"] for j in client.get("/jobs").json()]
        window.undo_button.click()
        assert target in [j["id"] for j in client.get("/jobs").json()]
        window.close()


def test_library_use_and_edit_keep_generation_configuration(tmp_path, qapp, monkeypatch):
    from voice_workbench.gui import Window
    from voice_workbench.library import PresetDialog, ReferencePicker
    data = tmp_path / "data"
    voice = index_voice(tmp_path)
    ConfigStore(data).save_voice(voice)
    with TestClient(create_app(data, runner=Runner())) as client:
        window = Window(8789, data, autostart=False)
        window.timer.stop()
        def call(action, success=None, failure=None):
            value = action(client)
            if success:
                success(value)
        monkeypatch.setattr(window, "call", call)
        window.poll()
        preset = next(v for v in window.voices if v["id"] == voice["id"])
        window.set_page(1)
        window.use_library_voice(preset)
        assert window.tabs.currentIndex() == 0 and window.current_voice()["id"] == voice["id"]
        dialog = window.open_dialog(PresetDialog(window, preset))
        assert not window.dialog_shade.isHidden()
        dialog.name.setText("旁白声音")
        dialog.controls.speed.setValue(1.2)
        dialog.save()
        assert window.dialog_shade.isHidden()
        saved = next(v for v in ConfigStore(data).voices() if v["id"] == voice["id"])
        assert saved["name"] == "旁白声音" and saved["speed"] == 1.2
        reference = next(r for r in window.references if r["reference_audio"] == saved["reference_audio"])
        picker = window.open_dialog(ReferencePicker(window, window.model_controls))
        picker.choose({**reference, "reference_text": "从参考库带入的原文"})
        picker.apply()
        assert window.dialog_shade.isHidden()
        assert window.model_controls.reference_text.toPlainText() == "从参考库带入的原文"
        previous = list(window.library.tickets)
        window.library.search.setText("不存在的声音")
        assert all(ticket.isHidden() for ticket in previous)
        assert not window.library.tickets
        window.library.search.clear()
        assert window.library.tickets
        window.close()
