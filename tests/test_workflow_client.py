from fastapi.testclient import TestClient

from integration.local_voice_client import is_local_service, run_local_job
from voice_workbench.api import create_app
from test_service import ControlledRunner


class Session:
    def __init__(self, client):
        self.client = client

    def get(self, url, **kwargs):
        return self.client.get(url)

    def post(self, url, **kwargs):
        kwargs.pop("timeout", None)
        return self.client.post(url, **kwargs)


def test_workflow_keeps_partial_success_and_reports_original_subtitle_position(tmp_path):
    runner = ControlledRunner()
    runner.fail_once.add("bad")
    tasks = [(36, "00:01:00.00", "good", "unused", 0), (37, "00:01:05.00", "bad", "unused", 0)]
    with TestClient(create_app(tmp_path, runner)) as client:
        session = Session(client)
        assert is_local_service(session, "http://127.0.0.1:8765")
        results = run_local_job(session, "http://127.0.0.1:8765", "default", tasks)
        assert results[0][0].startswith("http://127.0.0.1:8765/audio/")
        assert results[0][1] is None
        assert results[1][0] is None
        assert "第 37 段 [00:01:05.00]" in results[1][1]
        assert "显存不足" in results[1][1]


def test_workflow_stop_cancels_submitted_service_job(tmp_path):
    runner = ControlledRunner()
    runner.block.set()
    with TestClient(create_app(tmp_path, runner)) as client:
        results = run_local_job(Session(client), "http://127.0.0.1:8765", "default",
                                [(1, "00:00:00.00", "hello", "unused", 0)], cancel_check=lambda: True)
        assert results[0][0] is None
        assert "任务已取消" in results[0][1]
        jobs = client.get("/jobs").json()
        assert jobs[0]["status"] == "cancelled"


def test_remote_legacy_service_is_not_probed():
    class NoRequests:
        def get(self, *args, **kwargs):
            raise AssertionError("must not probe a remote legacy service")
    assert not is_local_service(NoRequests(), "http://other-service:8000")
