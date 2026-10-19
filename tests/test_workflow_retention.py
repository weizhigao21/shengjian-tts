"""Use disposable WAV fixtures to verify transfer ownership and durable metadata."""
import importlib.util
import asyncio
import io
import sqlite3
import time
import wave
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request
import pytest

from integration.local_voice_client import generate_local_tasks
from voice_workbench.api import create_app
from voice_workbench.config import atomic_json
from voice_workbench.manager import Manager
from voice_workbench.store import Store
from test_service import ControlledRunner, make_wave, wait_job
from test_workflow_client import Session


def submit(client, **kwargs):
    body = {"title": "双语工作流配音", "segments": [{"text": "hello", "timestamp": "00:01"}], **kwargs}
    result = client.post("/jobs", json=body)
    assert result.status_code == 202, result.text
    job_id = result.json()["job_id"]
    return wait_job(lambda key: client.get(f"/jobs/{key}").json(), job_id)


def test_old_ungrouped_submissions_and_legacy_calls_never_create_sidebar_works(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        for i in range(12):
            job = submit(client, segments=[{"text": str(i)}])
            clip = job["segments"][0]
            assert client.get(clip["audio_url"]).status_code == 200
            recorded = client.get(f"/jobs/{job['id']}").json()["segments"][0]
            assert recorded["audio"] is None and recorded["status"] == "done"
            assert recorded["audio_release_reason"] == "delivered"
        for endpoint, text in [("infer_single", {"text": "single"}), ("infer_bulk", {"texts": ["one", "two"]})]:
            result = client.post("/" + endpoint, json=text).json()
            for url in result.get("audio_urls", [result.get("audio_url")]):
                assert client.get(url).status_code == 200
        assert client.get("/jobs").json() == []
        assert client.get("/generated-audio").json() == []
        assert len(client.get("/workflow/records").json()) == 15
        assert not list((tmp_path / "workflow-audio").glob("*.wav"))
        assert not list((tmp_path / "audio").glob("*.wav"))
        assert Store.read_recent(tmp_path) == []
        assert client.app.state.manager.store.db.execute("SELECT count(*) FROM cache").fetchone()[0] == 0


def test_ack_is_idempotent_and_full_get_does_not_delete_before_workflow_saves(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        job = submit(client, delivery_ack=True)
        clip = job["segments"][0]
        url = clip["audio_url"]
        path = tmp_path / "workflow-audio" / clip["audio"]
        data = client.get(url).content
        assert path.is_file()
        with wave.open(io.BytesIO(data), "rb") as audio:
            assert audio.getnframes() == 16000
        output = tmp_path / "workflow-output.wav"
        output.write_bytes(data)
        assert client.post(url + "/release").json()["released"] == [f"{job['id']}:1"]
        assert client.post(url + "/release").json()["released"] == []
        assert not path.exists() and output.is_file()
        assert client.get(url).status_code == 404
        retained = client.get("/workflow/records").json()[0]
        assert retained["text"] == "hello" and retained["timestamp"] == "00:01"
        assert retained["voice"]["backend"] == "windows_sapi"
        assert retained["provenance"]["preset_used"] and retained["created"] and retained["duration"] == 1
        assert retained["status"] == "done" and retained["audio"] is None


def test_head_range_and_interrupted_stream_do_not_consume_old_client_audio(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        job = submit(client)
        clip = job["segments"][0]
        path = tmp_path / "workflow-audio" / clip["audio"]
        assert client.head(clip["audio_url"]).status_code == 200 and path.exists()
        ranged = client.get(clip["audio_url"], headers={"Range": "bytes=0-43"})
        assert ranged.status_code == 206 and len(ranged.content) == 44 and path.exists()
        route = next(r for r in client.app.routes if r.path == "/workflow/audio/{filename}")
        scope = {"type": "http", "method": "GET", "headers": [], "app": client.app,
                 "extensions": {}, "asgi": {"version": "3.0", "spec_version": "2.4"}}
        response = route.endpoint(clip["audio"], Request(scope))
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")
        with pytest.raises(OSError, match="disconnected"):
            asyncio.run(response(scope, receive, send))
        assert path.exists()
        assert client.get(clip["audio_url"]).status_code == 200 and not path.exists()


class Cache:
    def __init__(self):
        self.saved = []
    def save_audio_cache(self, *args, **kwargs):
        self.saved.append((args, kwargs))


def test_adapter_acknowledges_only_valid_successful_downloads_and_keeps_partial_results(tmp_path):
    runner = ControlledRunner()
    runner.fail_once.add("bad")
    tasks = [(1, "00:01", "good", str(tmp_path), 7), (2, "00:02", "download fails", str(tmp_path), 7),
             (3, "00:03", "bad", str(tmp_path), 7)]
    with TestClient(create_app(tmp_path / "data", runner)) as client:
        def filename(index, *args):
            return str(tmp_path / f"out-{index}.wav")
        downloads = []
        def download(url, target):
            content = client.get(url).content
            downloads.append(url)
            if len(downloads) == 2:
                raise OSError("disk write failed")
            Path(target).write_bytes(content)
        cache = Cache()
        results = generate_local_tasks(Session(client), "http://testserver", "default", tasks, cache, "voice",
                                       filename, download, workflow_context={"key": "one-run", "title": "双语配音 · test"})
        assert [r[0] for r in results] == [True, False, False]
        assert "disk write failed" in results[1][1] and "第 3 段" in results[2][1]
        records = sorted(client.get("/workflow/records").json(), key=lambda r: r["idx"])
        assert records[0]["audio"] is None and records[1]["audio"] and records[2]["error"]
        assert len(cache.saved) == 1 and (tmp_path / "out-1.wav").is_file()
        assert client.get("/jobs").json() == []


def test_cleanup_never_deletes_normal_cache_with_same_voice_and_text(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        normal = submit(client, title="ordinary work")
        normal_path = tmp_path / "audio" / normal["segments"][0]["audio"]
        original = normal_path.read_bytes()
        ephemeral = [submit(client, delivery_ack=True) for _ in range(3)]
        assert len({j["segments"][0]["audio"] for j in ephemeral}) == 3
        result = client.post("/workflow/records/cleanup").json()
        assert len(result["released"]) == 3 and not result["errors"]
        assert normal_path.read_bytes() == original
        assert len(client.get("/jobs").json()) == 1 and len(client.get("/generated-audio").json()) == 1
        repeat = submit(client, title="ordinary repeat")
        assert repeat["segments"][0]["cached"] == 1


def test_cancel_one_generation_releases_only_its_own_pending_clips(tmp_path):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        first = submit(client, workflow_key="run", delivery_ack=True)
        second = submit(client, workflow_key="run", delivery_ack=True)
        url = second["segments"][-1]["audio_url"]
        client.post(f"/jobs/{first['id']}/generations/1/cancel").raise_for_status()
        records = client.get(f"/jobs/{first['id']}").json()["segments"]
        assert records[0]["audio"] is None and records[1]["audio"]
        assert client.get(url).status_code == 200


def test_expiry_waits_for_entire_batch_and_survives_restart(tmp_path):
    class PausedSecond(ControlledRunner):
        def generate(self, request, cancelled):
            if request["text"] == "two":
                self.block.set()
            super().generate(request, cancelled)
    runner = PausedSecond()
    manager = Manager(tmp_path, runner)
    try:
        job_id = manager.submit("workflow", "default", [{"text": "one"}, {"text": "two"}],
                                workflow_key="run", delivery_ack=True)
        deadline = time.monotonic() + 2
        while not runner.block.is_set() and time.monotonic() < deadline:
            time.sleep(.01)
        first = manager.store.get(job_id)["segments"][0]
        assert first["status"] == "done" and first["audio_expires"] is None
        runner.block.clear()
        job = wait_job(manager.store.get, job_id)
        assert all(s["audio_expires"] for s in job["segments"])
        with manager.store.lock, manager.store.db:
            manager.store.db.execute("UPDATE segments SET audio_expires=? WHERE job_id=?", (time.time()-1, job_id))
    finally:
        runner.block.clear()
        manager.close()
    manager = Manager(tmp_path, ControlledRunner())
    try:
        clips = manager.store.get(job_id)["segments"]
        assert all(s["audio"] is None and s["audio_release_reason"] == "expired" for s in clips)
        assert not list((tmp_path / "workflow-audio").glob("*.wav"))
    finally:
        manager.close()


def test_old_database_migration_hides_records_and_history_cleanup_preserves_shared_desktop_files(tmp_path):
    manager = Manager(tmp_path, ControlledRunner())
    ordinary = manager.submit("my work", "default", [{"text": "shared"}])
    normal = wait_job(manager.store.get, ordinary)
    historical = manager.submit("old work", "default", [{"text": "shared"}, {"text": "unique"}])
    old = wait_job(manager.store.get, historical)
    manager.close()
    shared = tmp_path / "audio" / normal["segments"][0]["audio"]
    unique = tmp_path / "audio" / old["segments"][1]["audio"]
    with sqlite3.connect(tmp_path / "tasks.sqlite3") as db:
        db.execute("UPDATE jobs SET title='双语工作流配音' WHERE id=?", (historical,))
        db.execute("ALTER TABLE jobs DROP COLUMN record_only")
    assert [j["id"] for j in Store.read_recent(tmp_path)] == [ordinary]
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        assert [j["id"] for j in client.get("/jobs").json()] == [ordinary]
        result = client.post("/workflow/records/cleanup").json()
        assert len(result["released"]) == 2 and not result["errors"]
        assert shared.is_file() and not unique.exists()
        records = client.get("/workflow/records").json()
        assert len(records) == 2 and all(r["audio"] is None and r["status"] == "done" for r in records)
        assert client.app.state.manager.store.cache_get(unique.stem) is None
        assert client.app.state.manager.store.cache_get(shared.stem)


def test_locked_file_cleanup_can_retry_without_losing_metadata(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        job = submit(client, delivery_ack=True)
        clip = job["segments"][0]
        original = Path.unlink
        def locked(path, *args, **kwargs):
            if path.name == clip["audio"]:
                raise PermissionError("file locked")
            return original(path, *args, **kwargs)
        with monkeypatch.context() as context:
            context.setattr(Path, "unlink", locked)
            failed = client.post(clip["audio_url"] + "/release").json()
        assert failed["errors"] and not failed["released"]
        assert client.get(f"/jobs/{job['id']}").json()["segments"][0]["audio"] == clip["audio"]
        assert client.post(clip["audio_url"] + "/release").json()["released"]


def test_history_cleanup_preserves_audio_used_as_a_cloning_reference(tmp_path):
    manager = Manager(tmp_path, ControlledRunner())
    job_id = manager.submit("historical", "default", [{"text": "reference"}])
    job = wait_job(manager.store.get, job_id)
    path = tmp_path / "audio" / job["segments"][0]["audio"]
    with manager.store.lock, manager.store.db:
        manager.store.db.execute("UPDATE jobs SET record_only=1 WHERE id=?", (job_id,))
    value = manager.config.read()
    atomic_json(manager.config.path, {**value, "references": [{"id": "old-reference", "name": "clone", "reference_audio": str(path)}]})
    try:
        assert manager.workflow_audio.cleanup_all()["released"] == [f"{job_id}:1"]
        assert path.is_file() and manager.store.get(job_id)["segments"][0]["audio"] is None
        assert manager.store.cache_get(path.stem)
    finally:
        manager.close()


@pytest.mark.parametrize("corrupted", [False, True])
def test_adapter_does_not_ack_missing_or_invalid_saved_audio(tmp_path, corrupted):
    with TestClient(create_app(tmp_path / "data", ControlledRunner())) as client:
        def download(url, target):
            client.get(url).raise_for_status()
            if corrupted:
                Path(target).write_bytes(b"not a wave")
        cache = Cache()
        result = generate_local_tasks(Session(client), "http://testserver", "default",
            [(1, "", "hello", str(tmp_path), 0)], cache, "voice", lambda *args: str(tmp_path / "out.wav"), download)
        assert not result[0][0] and not cache.saved
        assert client.get("/workflow/records").json()[0]["audio"]


def test_lost_ack_does_not_mark_saved_output_failed_or_generate_again(tmp_path):
    with TestClient(create_app(tmp_path / "data", ControlledRunner())) as client:
        class LostAck(Session):
            def post(self, url, **kwargs):
                if url.endswith("/release"):
                    raise TimeoutError("ack lost")
                return super().post(url, **kwargs)
        def download(url, target):
            Path(target).write_bytes(client.get(url).content)
        result = generate_local_tasks(LostAck(client), "http://testserver", "default",
            [(1, "", "hello", str(tmp_path), 0)], Cache(), "voice", lambda *args: str(tmp_path / "out.wav"), download)
        assert result[0][0] and "到期清理" in result[0][1]
        assert (tmp_path / "out.wav").is_file()
        assert client.app.state.manager.runner.calls == ["hello"]


@pytest.mark.skipif(not Path(r"G:\code\双语音声工作流\src\steps\local_voice_client.py").is_file(), reason="paired checkout not present")
def test_existing_workflow_adapter_consumes_audio_without_new_client_fields(tmp_path):
    spec = importlib.util.spec_from_file_location("existing_adapter", r"G:\code\双语音声工作流\src\steps\local_voice_client.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    with TestClient(create_app(tmp_path, ControlledRunner())) as client:
        results = adapter.run_local_job(Session(client), "http://testserver", "default", [(1, "", "old client", "unused", 0)])
        assert not results[0][1]
        response = client.get(results[0][0])
        assert response.status_code == 200
        # Once installed, the paired adapter uses acknowledgement instead.
        if "delivery_ack" not in adapter.run_local_job.__code__.co_consts:
            if list((tmp_path / "workflow-audio").glob("*.wav")):
                assert client.post(results[0][0] + "/release").json()["released"]
        assert not list((tmp_path / "workflow-audio").glob("*.wav"))
        assert client.get("/jobs").json() == []
