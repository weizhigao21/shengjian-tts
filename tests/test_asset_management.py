"""Asset operations use temporary fixtures, never the user's model or audio files."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QMessageBox

from voice_workbench.api import create_app
from voice_workbench.config import atomic_json
from voice_workbench.errors import VoiceError
from voice_workbench.gui import Window
from voice_workbench.manager import Manager
from voice_workbench.model_management import ModelManager, download_url
from voice_workbench.naming import work_title
from voice_workbench.store import Store
from test_model_capabilities import gguf
from test_service import ControlledRunner, wait_job


def test_unnamed_work_uses_creation_time_and_first_five_text_characters(tmp_path):
    with TestClient(create_app(tmp_path, runner=ControlledRunner())) as client:
        draft = client.post("/jobs/draft", json={}).json()
        target = draft["id"]
        assert draft["title"] == "未命名配音"
        stamp = datetime.fromtimestamp(draft["created"]).strftime("%Y-%m-%d %H:%M:%S")
        result = client.patch(f"/jobs/{target}/document", json={"title": "", "script": "[00:05.00] 你好，欢迎来到这里。"}).json()
        assert result["title"] == stamp + " · 你好，欢迎"
        job = client.get(f"/jobs/{target}").json()
        client.post(f"/jobs/{target}/generate", json={"title": job["title"], "segments": [{"text": "新文本"}]})
        assert wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)["title"] == result["title"]
        client.patch(f"/jobs/{target}/document", json={"title": "用户自己的名字", "script": "完全不同的文字"})
        assert client.get(f"/jobs/{target}").json()["title"] == "用户自己的名字"
    assert work_title("", "1\n00:00:00,000 --> 00:00:01,000\n今天是晴天呀", draft["created"]) == stamp + " · 今天是晴天"
    with TestClient(create_app(tmp_path, runner=ControlledRunner())) as client:
        assert client.get(f"/jobs/{target}").json()["title"] == "用户自己的名字"
        response = client.post("/jobs", json={"segments": [{"text": "直接开始生成"}]}).json()
        assert response["title"].endswith(" · 直接开始生")


def test_auto_named_draft_updates_prefix_while_typing_until_manually_named(tmp_path):
    with TestClient(create_app(tmp_path, runner=ControlledRunner())) as client:
        draft = client.post("/jobs/draft", json={}).json()
        target = draft["id"]
        first = client.patch(f"/jobs/{target}/document", json={"title": draft["title"], "script": "你"}).json()["title"]
        assert first.endswith(" · 你")
        second = client.patch(f"/jobs/{target}/document", json={"title": first, "script": "你好欢迎使用"}).json()["title"]
        assert second.endswith(" · 你好欢迎使")
        client.post(f"/jobs/{target}/generate", json={"title": second, "segments": [{"text": "你好欢迎使用"}]})
        wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)
        third = client.patch(f"/jobs/{target}/document", json={"title": second, "script": "修改后的新文本"}).json()["title"]
        assert third.endswith(" · 修改后的新")
        client.patch(f"/jobs/{target}", json={"title": "用户命名"})
        assert client.patch(f"/jobs/{target}/document", json={"title": "用户命名", "script": "其他文字"}).json()["title"] == "用户命名"


def test_audio_metadata_and_single_bulk_all_deletion_preserve_shared_files_and_work(tmp_path):
    runner = ControlledRunner()
    with TestClient(create_app(tmp_path, runner=runner)) as client:
        target = client.post("/jobs", json={"title": "共用缓存的作品", "script": "原始草稿", "segments": [{"text": "相同文本"}]}).json()["job_id"]
        wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)
        client.post(f"/jobs/{target}/generate", json={"segments": [{"text": "相同文本"}]})
        job = wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)
        rows = client.get("/generated-audio").json()
        assert len(rows) == 2 and all(r["duration"] == 1 for r in rows)
        assert all(r["mode"] == "内置音色" and r["model"] == "Windows 系统声音" for r in rows)
        original_preset = rows[0]["provenance"]["preset_name"]
        assert original_preset and rows[0]["provenance"]["preset_used"]
        m = client.app.state.manager
        voice = m.config.resolve("default")[0]
        m.config.save_voice({**voice, "name": "后来改名的预设"})
        assert client.get("/generated-audio").json()[0]["provenance"]["preset_name"] == original_preset
        audio = tmp_path / "audio" / rows[0]["audio"]
        assert job["segments"][0]["audio"] == job["segments"][1]["audio"]
        assert client.post("/generated-audio/delete", json={"ids": [rows[0]["id"]]}).json()["deleted"] == [rows[0]["id"]]
        assert audio.is_file() and len(client.get("/generated-audio").json()) == 1
        assert client.get(f"/jobs/{target}").json()["total"] == 1
        # Removing a work from Recents retains its audio until the audio manager deletes it.
        client.delete(f"/jobs/{target}")
        remaining = client.get("/generated-audio").json()
        assert remaining[0]["work_deleted"] == 1
        assert client.post("/generated-audio/delete", json={"ids": [remaining[0]["id"]]}).json()["errors"] == []
        assert not audio.exists()
        assert m.store.cache_get(audio.stem) is None
        client.post(f"/jobs/{target}/restore")
        assert client.get(f"/jobs/{target}").json()["script"] == "原始草稿"
        assert client.get(f"/jobs/{target}").json()["segments"] == []
        # Deleted segment indexes remain reserved; later generation must still run correctly.
        client.post(f"/jobs/{target}/generate", json={"segments": [{"text": "相同文本"}, {"text": "其他文本"}], "overrides": {"speed": 1.2}})
        regenerated = wait_job(lambda key: client.get(f"/jobs/{key}").json(), target)
        assert [s["idx"] for s in regenerated["segments"]] == [3, 4]
        assert runner.calls == ["相同文本", "相同文本", "其他文本"]
        assert regenerated["segments"][0]["provenance"]["preset_modified"] is True
        assert client.post("/generated-audio/delete", json={"all": True}).status_code == 200
        assert client.get("/generated-audio").json() == []
        assert not list((tmp_path / "audio").glob("*.wav"))
        assert client.post("/generated-audio/delete", json={"ids": ["unknown:1"]}).status_code == 400
        assert client.get(f"/jobs/{target}").json()["title"] == "共用缓存的作品"


def test_audio_delete_failure_keeps_record_and_cache(tmp_path, monkeypatch):
    manager = Manager(tmp_path, ControlledRunner())
    try:
        target = manager.submit("保留", "default", [{"text": "文件被占用"}])
        wait_job(manager.store.get, target)
        row = manager.generated_audio()[0]
        original = Path.unlink
        def locked(path, *args, **kwargs):
            if path.name == row["audio"]:
                raise PermissionError("file locked")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", locked)
        result = manager.delete_generated_audio([row["id"]])
        assert result["deleted"] == [] and "file locked" in result["errors"][0]
        assert len(manager.generated_audio()) == 1 and manager.store.cache_get(Path(row["audio"]).stem)
    finally:
        manager.close()


def model_fixture(tmp_path, *, pad=0):
    model = tmp_path / "small.gguf"
    gguf(model, {"schema_version": 1, "family": "kokoro_tts", "languages": ["zh"], "options": {"request": []}}, voices=["zf_xiaobei"])
    content = model.read_bytes() + b"\0" * pad
    entry = {"id": "test-kokoro", "name": "Kokoro 测试", "family": "kokoro_tts", "precision": "Q8",
             "remotePath": "Test/small.gguf", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    manager = Manager(tmp_path / "data", ControlledRunner())
    # Keep this test independent of any locally discovered yovoice installation.
    config = manager.config.read()
    atomic_json(manager.config.path, {"voices": [v for v in config["voices"] if v["id"] == "default"]})
    exe = tmp_path / "audiocpp_server.exe"
    exe.write_bytes(b"fixture")
    manager.models.set_runtime(str(exe))
    return manager, model, content, entry


@pytest.mark.parametrize("resume_supported", [True, False])
def test_download_resumes_or_restarts_then_verifies_and_registers(tmp_path, resume_supported):
    manager, model, content, entry = model_fixture(tmp_path)
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers["range"] == "bytes=100-"
        if resume_supported:
            return httpx.Response(206, content=content[100:], headers={"Content-Range": f"bytes 100-{len(content)-1}/{len(content)}"})
        return httpx.Response(200, content=content)
    downloads = ModelManager(manager, [entry], lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    manager.models.close()
    manager.models = downloads
    part = downloads.root / "small.gguf.part"
    part.write_bytes(content[:100])
    try:
        assert downloads.listing()["catalog"][0]["status"] == "paused"
        downloads.start_download(entry["id"])
        downloads.threads[entry["id"]].join(3)
        assert downloads.downloads[entry["id"]]["status"] == "done"
        assert (downloads.root / "small.gguf").read_bytes() == content and not part.exists()
        assert downloads.downloads[entry["id"]]["resumed_from"] == (100 if resume_supported else 0)
        native = next(v for v in manager.config.voices() if v.get("backend") == "audio_cpp")
        assert native["is_template"] and native["family"] == "kokoro_tts"
        assert native["engine_executable"].endswith("audiocpp_server.exe")
        target = manager.submit("模型默认设置", native["id"], [{"text": "你好"}])
        wait_job(manager.store.get, target)
        assert manager.generated_audio()[0]["provenance"]["preset_used"] is False
        assert downloads.listing()["catalog"][0]["installed"]
    finally:
        manager.close()


@pytest.mark.parametrize("problem", ["sha", "range", "short", "oversize", "network"])
def test_failed_download_is_not_registered_and_reports_reason(tmp_path, problem):
    manager, _, content, entry = model_fixture(tmp_path)
    if problem == "sha":
        entry["sha256"] = "0" * 64
    def handle(request):
        if problem == "network":
            raise httpx.ConnectError("connection unavailable", request=request)
        if problem == "range":
            return httpx.Response(206, content=content[100:], headers={"Content-Range": f"bytes 99-{len(content)-1}/{len(content)}"})
        return httpx.Response(200, content=content[:-50] if problem == "short" else content + b"too large" if problem == "oversize" else content)
    downloads = ModelManager(manager, [entry], lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    manager.models.close()
    manager.models = downloads
    if problem == "range":
        (downloads.root / "small.gguf.part").write_bytes(content[:100])
    try:
        downloads.start_download(entry["id"])
        downloads.threads[entry["id"]].join(3)
        state = downloads.downloads[entry["id"]]
        assert state["status"] == "failed" and state["error"]
        assert downloads.installed() == [] and not (downloads.root / "small.gguf").exists()
        if problem == "sha":
            assert not (downloads.root / "small.gguf.part").exists()
    finally:
        manager.close()


def test_download_pause_keeps_partial_file_and_close_stops_thread(tmp_path):
    manager, _, content, entry = model_fixture(tmp_path, pad=600000)
    release = threading.Event()
    yielded = threading.Event()
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            yield content[:262144]
            yielded.set()
            release.wait(3)
            yield content[262144:]
    downloads = ModelManager(manager, [entry], lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))))
    manager.models.close()
    manager.models = downloads
    try:
        downloads.start_download(entry["id"])
        assert yielded.wait(2)
        downloads.pause(entry["id"])
        release.set()
        downloads.threads[entry["id"]].join(3)
        assert downloads.downloads[entry["id"]]["status"] == "paused"
        assert (downloads.root / "small.gguf.part").stat().st_size == 262144
        assert downloads.installed() == []
    finally:
        release.set()
        manager.close()
    assert not downloads.threads[entry["id"]].is_alive()


def test_model_removal_is_scoped_can_keep_external_file_and_rejects_queued_use(tmp_path):
    manager, model, _, _ = model_fixture(tmp_path)
    runner = manager.runner
    try:
        installed = manager.models.register(model)
        assert not installed["managed"] and "不支持声音克隆" in installed["description"]
        native = next(v for v in manager.config.voices() if v.get("backend") == "audio_cpp")
        runner.block.set()
        target = manager.submit("排队", native["id"], [{"text": "你好"}])
        with pytest.raises(VoiceError, match="模型正在使用"):
            manager.models.remove(installed["id"], True)
        manager.cancel(target)
        runner.block.clear()
        deadline = time.monotonic() + 3
        while manager.current and time.monotonic() < deadline:
            time.sleep(.01)
        manager.models.remove(installed["id"], False)
        assert model.exists() and manager.models.installed() == []
        second = manager.models.register(model)
        assert str(model.resolve()).lower() not in manager.config.read()["removed_models"]
        manager.models.remove(second["id"], True)
        assert not model.exists()
        assert (tmp_path / "audiocpp_server.exe").exists()
    finally:
        runner.block.clear()
        manager.close()


def test_catalog_sources_and_import_only_entries():
    entry = {"remotePath": "Folder/model.gguf", "revision": "commit"}
    assert download_url(entry, "huggingface") == "https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/commit/Folder/model.gguf"
    assert "hf-mirror.com" in download_url(entry, "mirror")
    assert "HereIsMark/audio.cpp-gguf/resolve/master" in download_url(entry, "modelscope")
    with pytest.raises(VoiceError):
        download_url({"remotePath": ""}, "mirror")


def test_model_cannot_be_deleted_between_submission_validation_and_enqueue(tmp_path, monkeypatch):
    import voice_workbench.manager as module
    manager, model, _, _ = model_fixture(tmp_path)
    installed = manager.models.register(model)
    native = next(v for v in manager.config.voices() if v.get("backend") == "audio_cpp")
    entered, release = threading.Event(), threading.Event()
    original = module.check_voice
    def validating(voice):
        original(voice)
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(module, "check_voice", validating)
    manager.runner.block.set()
    submissions, errors = [], []
    submit = threading.Thread(target=lambda: submissions.append(manager.submit("并发", native["id"], [{"text": "你好"}])))
    def deleting():
        try:
            manager.models.remove(installed["id"], True)
        except VoiceError as exc:
            errors.append(exc.code)
    deletion = threading.Thread(target=deleting)
    try:
        submit.start()
        assert entered.wait(2)
        deletion.start()
        release.set()
        submit.join(3)
        deletion.join(3)
        assert submissions and errors == ["ENGINE_BUSY"] and model.exists()
        manager.cancel(submissions[0])
    finally:
        release.set()
        manager.runner.block.clear()
        submit.join(3)
        if deletion.ident:
            deletion.join(3)
        manager.close()


def test_settings_multi_selection_search_and_buttons_keep_state_on_refresh(tmp_path, qtbot, monkeypatch):
    manager = Manager(tmp_path, ControlledRunner())
    target = manager.submit("管理测试", "default", [{"text": "第一句"}, {"text": "第二句"}])
    wait_job(manager.store.get, target)
    models, audios = manager.models.listing(), manager.generated_audio()
    manager.close()
    window = Window(8794, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    settings = window.settings
    settings.model_data = models
    settings.audio_data = audios
    settings.render_models()
    settings.render_audio()
    from voice_workbench.runner import validate_wave
    # Deletion temporarily releases the main player's source. A surviving clip must reload.
    store = Store(tmp_path)
    job = store.get(target)
    store.close()
    window.selected_job = target
    window.render_job(job)
    surviving = window.current_segment()
    assert surviving and validate_wave(tmp_path / "audio" / surviving["audio"])
    window.player.setSource(QUrl())
    window.preview_segment()
    assert window.player.source() == QUrl.fromLocalFile(str(tmp_path / "audio" / surviving["audio"]))
    window.show()
    window.set_page(3)
    settings.set_section(1)
    assert settings.audio_table.rowCount() == 2
    checkbox = settings.audio_table.item(0, 0)
    checkbox.setCheckState(Qt.CheckState.Checked)
    assert settings.delete_selected.isEnabled() and len(settings.selected_ids) == 1
    before = settings.audio_table.cellWidget(0, 8)
    settings.render_audio()
    assert settings.audio_table.cellWidget(0, 8) is before
    assert settings.audio_table.item(0, 0).checkState() == Qt.CheckState.Checked
    qtbot.mouseClick(settings.select_all, Qt.MouseButton.LeftButton)
    assert len(settings.selected_ids) == 2
    settings.audio_search.setText("第一句")
    assert settings.audio_table.rowCount() == 1 and len(settings.selected_ids) == 2
    shown = []
    monkeypatch.setattr(window, "show_details", shown.append)
    qtbot.mouseClick(settings.audio_table.cellWidget(0, 7), Qt.MouseButton.LeftButton)
    assert "第一句" in shown[0] and "生成时配置" in shown[0] and "声音" in shown[0]
    captured = []
    class Response:
        is_error = False
        def json(self):
            return {"deleted": [], "errors": []}
    class Client:
        def post(self, path, json):
            captured.append((path, json))
            return Response()
    window.call = lambda action, success=None, failure=None: success(action(Client()))
    window.poll = lambda: None
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    qtbot.mouseClick(settings.delete_all, Qt.MouseButton.LeftButton)
    assert captured[0][1] == {"ids": [a["id"] for a in audios]}
    # Download progress updates retain the same action widget and current scroll position.
    settings.pages.setCurrentIndex(0)
    first = settings.catalog_table.cellWidget(0, 4)
    settings.model_data = deepcopy(models)
    next(m for m in settings.model_data["catalog"] if m["id"] == settings.visible_catalog[0]["id"]).update(status="failed", error="具体失败原因")
    settings.render_models()
    assert settings.catalog_table.cellWidget(0, 4) is first
    assert "具体失败原因" in settings.catalog_table.item(0, 3).toolTip()
    window.close()
