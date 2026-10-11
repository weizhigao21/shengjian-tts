from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest

from integration.local_voice_client import run_local_job
from scripts.install_workflow_adapter import prepare
from voice_workbench.api import create_app
from voice_workbench.errors import VoiceError
from voice_workbench.manager import Manager
from test_model_capabilities import kokoro
from test_service import ControlledRunner, make_wave, wait_job
from test_workflow_client import Session


def test_micro_batches_share_one_work_and_keep_their_own_results(tmp_path):
    runner = ControlledRunner()
    runner.fail_once.add("bad")
    context = {"key": "same-workflow-run", "title": "双语配音 · 示例字幕"}
    with TestClient(create_app(tmp_path, runner)) as client:
        session = Session(client)
        first = run_local_job(session, "http://testserver", "default",
                              [(1, "00:01", "bad", "unused", 0)], workflow_context=context)
        second = run_local_job(session, "http://testserver", "default",
                               [(2, "00:02", "second", "unused", 0), (3, "00:03", "third", "unused", 0)], workflow_context=context)
        third = run_local_job(session, "http://testserver", "default",
                              [(4, "00:04", "last", "unused", 0)], workflow_context=context)
        jobs = client.get("/jobs").json()
        assert len(jobs) == 1 and jobs[0]["title"] == context["title"]
        assert jobs[0]["total"] == 4 and jobs[0]["completed"] == 3
        assert first[0][1] and all(url and error is None for url, error in second + third)
        stored = client.get("/jobs/" + jobs[0]["id"]).json()["segments"]
        assert [s["generation"] for s in stored] == [1, 2, 2, 3]
        assert second[0][0].endswith(stored[1]["audio_url"])
        assert third[0][0].endswith(stored[3]["audio_url"])


def test_simultaneous_submissions_get_distinct_batches_in_one_work(tmp_path):
    runner = ControlledRunner()
    runner.block.set()
    with TestClient(create_app(tmp_path, runner)) as client:
        def submit(i):
            return client.post("/jobs", json={"workflow_key": "one-run", "segments": [{"text": str(i)}]}).json()
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(submit, range(10)))
        assert len({r["job_id"] for r in results}) == 1
        assert {r["generation"] for r in results} == set(range(1, 11))
        assert {r["segment_indices"][0] for r in results} == set(range(1, 11))
        runner.block.clear()


def test_batch_cancel_preserves_other_batches_and_new_run_is_separate(tmp_path):
    runner = ControlledRunner()
    runner.block.set()
    with TestClient(create_app(tmp_path, runner)) as client:
        one = client.post("/jobs", json={"workflow_key": "first-run", "segments": [{"text": "one"}]}).json()
        two = client.post("/jobs", json={"workflow_key": "first-run", "segments": [{"text": "two"}]}).json()
        client.post(f"/jobs/{one['job_id']}/generations/{two['generation']}/cancel")
        runner.block.clear()
        job = wait_job(lambda j: client.get("/jobs/" + j).json(), one["job_id"])
        assert [s["status"] for s in job["segments"]] == ["done", "cancelled"]
        assert client.get(one["status_url"]).json()["status"] == "done"
        assert client.get(two["status_url"]).json()["status"] == "cancelled"
        other = client.post("/jobs", json={"workflow_key": "next-run", "segments": [{"text": "other"}]}).json()
        assert other["job_id"] != one["job_id"]
        client.post("/jobs/" + other["job_id"] + "/cancel")
        refused = client.post("/jobs", json={"workflow_key": "next-run", "segments": [{"text": "stale next batch"}]})
        assert refused.status_code == 404 and "已取消" in refused.json()["msg"]
        client.delete("/jobs/" + one["job_id"])
        assert client.post("/jobs", json={"workflow_key": "first-run", "segments": [{"text": "do not resurrect"}]}).status_code == 404


def test_new_workflow_refuses_an_old_service_without_creating_more_tasks():
    class OldSession:
        def get(self, *args, **kwargs):
            class Response:
                status_code = 200
                def json(self):
                    return {"capabilities": ["async_jobs"]}
            return Response()
        def post(self, *args, **kwargs):
            raise AssertionError("must not flood an old server")
    result = run_local_job(OldSession(), "http://testserver", "default", [(1, "", "one", "unused", 0)],
                           workflow_context={"key": "new-run", "title": "字幕"})
    assert "更新并重启" in result[0][1]


class ParallelProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.maximum = 0
        self.calls = []
        self.hold = threading.Event()
        self.entered = threading.Event()
        self.fail = set()
        self.interrupts = 0

    def runner(self, root=None):
        probe = self
        class Runner:
            def generate(self, request, cancelled):
                with probe.lock:
                    probe.running += 1
                    probe.maximum = max(probe.maximum, probe.running)
                    probe.calls.append(request["text"])
                    probe.entered.set()
                try:
                    while probe.hold.is_set() and not cancelled.is_set():
                        time.sleep(.01)
                    if cancelled.is_set():
                        raise VoiceError("CANCELLED", "已取消")
                    with probe.lock:
                        if request["text"] in probe.fail:
                            probe.fail.remove(request["text"])
                            raise RuntimeError("CUDA out of memory")
                    time.sleep(.04)
                    make_wave(request["output"])
                finally:
                    with probe.lock:
                        probe.running -= 1
            def interrupt(self):
                probe.interrupts += 1
        return Runner()


def parallel_manager(tmp_path, monkeypatch, probe):
    monkeypatch.setattr("voice_workbench.manager.check_voice", lambda voice: None)
    manager = Manager(tmp_path, runner=probe.runner(), runner_factory=probe.runner)
    voice = {**manager.config.resolve("default")[0], **kokoro(tmp_path)}
    manager.config.save_voice(voice)
    manager.set_performance({"workflow_workers": 2, "cpu_threads": 4})
    return manager


def test_parallel_inference_is_bounded_and_normal_gui_generation_stays_serial(tmp_path, monkeypatch):
    probe = ParallelProbe()
    probe.hold.set()
    manager = parallel_manager(tmp_path, monkeypatch, probe)
    try:
        work = manager.submit("batch", "default", [{"text": f"batch-{i}"} for i in range(8)], workflow_key="batch-run")
        deadline = time.monotonic() + 3
        while probe.maximum < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert probe.maximum == 2 and len(manager.status()["active"]) == 2
        with pytest.raises(VoiceError, match="正在生成"):
            manager.set_performance({"workflow_workers": 4, "cpu_threads": 1})
        probe.hold.clear()
        assert wait_job(manager.store.get, work)["completed"] == 8
        deadline = time.monotonic() + 3
        while manager.current and time.monotonic() < deadline:
            time.sleep(.01)
        probe.maximum = 0
        normal = manager.submit("gui", "default", [{"text": "gui-one"}, {"text": "gui-two"}])
        assert wait_job(manager.store.get, normal)["completed"] == 2
        assert probe.maximum == 1
        probe.maximum = 0
        manager.submit("gui append", "default", [{"text": "appended-one"}, {"text": "appended-two"}], job_id=work)
        assert wait_job(manager.store.get, work)["completed"] == 10
        assert probe.maximum == 1
    finally:
        probe.hold.clear()
        manager.close()


def test_parallel_oom_retries_once_in_serial_and_cancel_stops_every_lane(tmp_path, monkeypatch):
    probe = ParallelProbe()
    probe.fail.add("oom")
    manager = parallel_manager(tmp_path, monkeypatch, probe)
    try:
        work = manager.submit("oom", "default", [{"text": "oom"}, {"text": "good"}, {"text": "later"}], workflow_key="oom-run")
        assert wait_job(manager.store.get, work, ("done",))["completed"] == 3
        assert probe.calls.count("oom") == 2 and work in manager.serial_jobs
        assert "降为单实例" in manager.status()["performance_notice"]
        probe.hold.set()
        cancel = manager.submit("cancel", "default", [{"text": f"cancel-{i}"} for i in range(4)], workflow_key="cancel-run")
        deadline = time.monotonic() + 3
        while len(manager.active) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        manager.cancel(cancel)
        job = wait_job(manager.store.get, cancel)
        assert {s["status"] for s in job["segments"]} == {"cancelled"}
    finally:
        probe.hold.clear()
        manager.close()


@pytest.mark.skipif(not Path(r"G:\code\双语音声工作流\src\steps\tts_utils.py").exists(), reason="requires the paired workflow checkout")
def test_adapter_can_be_reinstalled_without_repeated_context_arguments(tmp_path):
    target = Path(r"G:\code\双语音声工作流")
    modifications = prepare(target)
    for path, source in modifications:
        path = tmp_path / path.relative_to(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    repeated = prepare(tmp_path)
    assert [source for _, source in repeated] == [source for _, source in modifications]
