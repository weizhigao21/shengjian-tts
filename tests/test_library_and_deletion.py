import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sqlite3
import threading
import time
import pytest

from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.manager import Manager
from voice_workbench.store import Store
from test_inline_generation import index_voice, make_audio, Runner


@pytest.fixture
def no_legacy_imports(monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])


def wait_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("status") in ("done", "failed", "cancelled"):
            return job
        time.sleep(.01)
    raise AssertionError("Task did not settle")


@pytest.mark.parametrize("size", [80, 56])
def test_record_artwork_keeps_square_geometry_with_application_button_styles(qapp, qtbot, size):
    from PyQt6.QtWidgets import QWidget, QVBoxLayout
    from voice_workbench.library import RecordButton
    from voice_workbench.ui import STYLE
    container = QWidget()
    container.setStyleSheet(STYLE)
    layout = QVBoxLayout(container)
    cover = RecordButton("empty-library", lambda: None, ring=True)
    cover.setFixedSize(size, size)
    layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignCenter)
    qtbot.addWidget(container)
    container.resize(360, 220)
    container.show()
    qapp.processEvents()
    assert cover.width() == cover.height() == size
    container.setStyleSheet(STYLE)
    qapp.processEvents()
    assert cover.width() == cover.height() == size


def test_api_preset_import_retains_audio_and_reuses_identical_files(tmp_path, no_legacy_imports):
    voice = index_voice(tmp_path)
    source = Path(voice["reference_audio"])
    original = source.read_bytes()
    data = tmp_path / "data"
    with TestClient(create_app(data, runner=Runner())) as client:
        response = client.put(f"/voices/{voice['id']}", json=voice)
        assert response.status_code == 200
        saved = response.json()
        retained = Path(saved["reference_audio"])
        assert retained.parent == data / "voices" and retained.read_bytes() == original
        reference = client.post("/references", json={"name": "参考原声", "reference_audio": str(source)}).json()
        assert reference["reference_audio"] == str(retained)
        again = client.put("/voices/another", json={**voice, "id": "another"}).json()
        assert again["reference_audio"] == str(retained)
        assert len(list((data / "voices").glob("*.wav"))) == 1
        source.unlink()
        assert client.get("/references").json()[0]["error"] is None
        submission = client.post("/jobs", json={"voice_id": voice["id"], "segments": [{"text": "原文件已删除"}]})
        assert submission.status_code == 202
        job = wait_job(client, submission.json()["job_id"])
        assert job["status"] == "done" and job["voice"]["reference_audio"] == str(retained)
        assert next(v for v in ConfigStore(data).voices() if v["id"] == voice["id"])["reference_audio"] == str(retained)


def test_inline_audio_is_retained_before_queued_inference(tmp_path, no_legacy_imports):
    voice = index_voice(tmp_path)
    source = Path(voice["reference_audio"])
    original = source.read_bytes()
    class WaitingRunner(Runner):
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()
        def generate(self, request, cancelled):
            self.entered.set()
            assert self.release.wait(5)
            assert Path(request["voice"]["reference_audio"]).read_bytes() == original
            super().generate(request, cancelled)
        def interrupt(self):
            self.release.set()
    data = tmp_path / "data"
    saved = ConfigStore(data).save_voice({**voice, "reference_audio": ""})
    runner = WaitingRunner()
    with TestClient(create_app(data, runner=runner)) as client:
        submission = client.post("/jobs", json={"voice_id": saved["id"], "segments": [{"text": "排队生成"}],
                                                "overrides": {"reference_audio": str(source)}})
        assert submission.status_code == 202
        assert runner.entered.wait(5)
        source.unlink()
        runner.release.set()
        job = wait_job(client, submission.json()["job_id"])
        assert job["status"] == "done"
        assert Path(job["voice"]["reference_audio"]).parent == data / "voices"
        assert not next(v for v in ConfigStore(data).voices() if v["id"] == saved["id"])["reference_audio"]


def test_failed_reference_copy_leaves_configuration_and_library_unchanged(tmp_path, monkeypatch, no_legacy_imports):
    from voice_workbench.errors import VoiceError
    import voice_workbench.config as config
    source = tmp_path / "original.wav"
    make_audio(source)
    store = ConfigStore(tmp_path / "data")
    previous = store.read()
    def fail_copy(*args):
        raise OSError("磁盘无法写入")
    monkeypatch.setattr(config.shutil, "copyfile", fail_copy)
    with pytest.raises(VoiceError) as error:
        store.import_reference({"name": "导入失败", "reference_audio": str(source)})
    assert error.value.code == "REFERENCE_COPY_FAILED"
    assert store.read() == previous and source.is_file()
    assert not list((store.data_dir / "voices").iterdir())


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
