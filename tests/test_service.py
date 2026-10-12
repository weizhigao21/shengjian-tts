import json
from pathlib import Path
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore, voice_fingerprint
from voice_workbench.manager import Manager
from voice_workbench.store import Store
from voice_workbench.subtitles import parse_text, normalize_timestamp


def make_wave(path):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x10\x00" * 16000)


class ControlledRunner:
    def __init__(self):
        self.calls = []
        self.fail_once = set()
        self.block = threading.Event()

    def generate(self, request, cancelled):
        text = request["text"]
        self.calls.append(text)
        while self.block.is_set() and not cancelled.is_set():
            time.sleep(0.01)
        if cancelled.is_set():
            raise RuntimeError("cancelled")
        if text in self.fail_once:
            self.fail_once.remove(text)
            raise RuntimeError("CUDA out of memory")
        make_wave(Path(request["output"]))

    def interrupt(self):
        pass


def wait_job(get, job_id, expected=("done", "failed", "cancelled")):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = get(job_id)
        if job["status"] in expected:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not settle: {job}")


def test_partial_failure_retry_keeps_success_and_cache(tmp_path):
    runner = ControlledRunner()
    runner.fail_once.add("bad")
    manager = Manager(tmp_path, runner)
    try:
        job_id = manager.submit("test", "default", [{"text": "good"}, {"text": "bad"}])
        failed = wait_job(manager.store.get, job_id)
        assert failed["status"] == "failed"
        assert failed["completed"] == 1
        assert failed["segments"][1]["error"]["code"] == "GPU_OUT_OF_MEMORY"
        log_path = tmp_path / "logs" / f"{job_id}-2.json"
        assert "CUDA out of memory" in json.loads(log_path.read_text(encoding="utf-8"))["traceback"]
        first_audio = failed["segments"][0]["audio"]
        manager.retry(job_id)
        done = wait_job(manager.store.get, job_id, ("done",))
        assert done["segments"][0]["audio"] == first_audio
        assert runner.calls == ["good", "bad", "bad"]
        repeat_id = manager.submit("repeat", "default", [{"text": "good"}])
        repeat = wait_job(manager.store.get, repeat_id)
        assert repeat["segments"][0]["cached"] == 1
        assert runner.calls == ["good", "bad", "bad"]
        # 非零但损坏的缓存不得复用。
        (tmp_path / "audio" / first_audio).write_bytes(b"not-a-wave")
        repaired = manager.submit("repair", "default", [{"text": "good"}])
        assert wait_job(manager.store.get, repaired)["status"] == "done"
        assert runner.calls[-1] == "good" and len(runner.calls) == 4
    finally:
        manager.close()


def test_restart_recovers_only_interrupted_segments(tmp_path):
    config = ConfigStore(tmp_path)
    voice, fingerprint = config.resolve("default")
    store = Store(tmp_path)
    job_id = store.create("resume", voice, fingerprint, [{"text": "kept"}, {"text": "interrupted"}])
    store.next()
    store.finish(job_id, 1, audio="kept.wav")
    store.next()
    store.close()
    runner = ControlledRunner()
    manager = Manager(tmp_path, runner)
    try:
        job = wait_job(manager.store.get, job_id)
        assert job["status"] == "done"
        assert runner.calls == ["interrupted"]
        assert job["segments"][0]["audio"] == "kept.wav"
    finally:
        manager.close()


def test_cancel_does_not_turn_into_failure_and_can_retry(tmp_path):
    runner = ControlledRunner()
    runner.block.set()
    manager = Manager(tmp_path, runner)
    try:
        job_id = manager.submit("cancel", "default", [{"text": "first"}, {"text": "second"}])
        wait_job(manager.store.get, job_id, ("running",))
        manager.cancel(job_id)
        assert wait_job(manager.store.get, job_id)["status"] == "cancelled"
        runner.block.clear()
        manager.retry(job_id)
        assert wait_job(manager.store.get, job_id, ("done",))["completed"] == 2
    finally:
        manager.close()


def test_api_legacy_download_validation_and_async_error(tmp_path):
    runner = ControlledRunner()
    with TestClient(create_app(tmp_path, runner)) as client:
        assert client.get("/").json()["service"] == "local-voice-workbench"
        voice = next(v for v in client.get("/voices").json() if v["id"] == "default")
        response = client.post("/infer_single", json={"model_name": voice["model_name"], "text": "hello", "dl_url": "http://untrusted.invalid"})
        assert response.status_code == 200
        audio_url = response.json()["audio_url"]
        assert audio_url.startswith("http://testserver/workflow/audio/")
        assert client.get(audio_url).content[:4] == b"RIFF"
        bulk = client.post("/infer_bulk", json={"model_name": "default", "texts": ["one", "two"]}).json()
        assert len(bulk["audio_urls"]) == 2
        assert client.post("/jobs", json={"voice_id": "default", "segments": [{"text": " "}]}).status_code == 400
        assert client.post("/jobs", json={"segments": [{"text": "x" * 5001}]}).status_code == 422
        runner.fail_once.add("fail")
        result = client.post("/jobs", json={"voice_id": "default", "segments": [{"text": "pass"}, {"text": "fail"}]}).json()
        job = wait_job(lambda j: client.get(f"/jobs/{j}").json(), result["job_id"])
        assert job["completed"] == 1
        assert job["segments"][1]["error"]["code"] == "GPU_OUT_OF_MEMORY"
        assert client.get(job["segments"][1]["error"]["log"]).status_code == 200
        assert client.get("/audio/not-valid.wav").status_code == 404
        assert client.post("/jobs", headers={"Origin": "https://untrusted.invalid"}, json={}).status_code == 403
        changed = {k: v for k, v in voice.items() if k not in ("model_name", "model_tag", "error")}
        changed["speed"] = 1.2
        assert client.put("/voices/default", json=changed).status_code == 200
        assert client.post("/infer_single", json={"model_name": voice["model_name"], "text": "stale"}).json()["error"]["code"] == "VOICE_VERSION_CHANGED"


def test_two_services_cannot_share_a_database(tmp_path):
    manager = Manager(tmp_path, ControlledRunner())
    try:
        with pytest.raises(RuntimeError, match="已有"):
            Manager(tmp_path, ControlledRunner())
    finally:
        manager.close()


def test_parses_subtitle_timestamps_without_losing_bilingual_text():
    assert parse_text("[00:01.00][00:03.00]你好 hello")[1] == {"text": "你好 hello", "timestamp": "00:03.00"}
    assert parse_text("1\n00:00:01,000 --> 00:00:02,000\n你好\nhello\n")[0]["text"] == "你好 hello"
    assert parse_text("WEBVTT\n\n00:01.000 --> 00:02.000\nhello")[0]["timestamp"] == "00:01.000"
    assert normalize_timestamp("01:05.340") == "00:01:05.34"
    assert normalize_timestamp("01:01:05,340") == "01:01:05.34"
    assert normalize_timestamp("", 65000) == "00:01:05.00"
