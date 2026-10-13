"""Draft durability across asynchronous saves, navigation and failed generation."""
import json

from fastapi.testclient import TestClient
import pytest

from voice_workbench.api import create_app
from voice_workbench.desktop.draft_saves import DraftSaveQueue
from voice_workbench.gui import Window
from test_service import ControlledRunner


def test_old_ack_does_not_remove_new_snapshot_and_restart_recovers_it(tmp_path):
    path = tmp_path / "pending.json"
    queue = DraftSaveQueue(path)
    body = {"title": "作品", "script": "第一稿", "editor_state": {"mode": "article"}}
    first = queue.stage("work", body)
    body["script"] = "第二稿"
    latest = queue.stage("work", body)
    body["script"] = "外部修改"
    queue.acknowledge(first)
    restored = DraftSaveQueue(path)
    assert restored.pending["work"].body["script"] == "第二稿"
    restored.acknowledge(latest)
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_unreadable_journal_does_not_block_startup_or_destroy_original(tmp_path):
    path = tmp_path / "pending.json"
    path.write_bytes(b"{partial JSON")
    queue = DraftSaveQueue(path)
    assert queue.load_error and not queue.pending
    queue.stage("work", {"title": "作品", "script": "新草稿"})
    preserved = list(tmp_path.glob("pending-drafts-unreadable-*.json"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == b"{partial JSON"
    assert DraftSaveQueue(path).pending["work"].body["script"] == "新草稿"


def test_local_write_failure_retains_snapshot_and_retries(tmp_path, monkeypatch):
    queue = DraftSaveQueue(tmp_path / "pending.json")
    with monkeypatch.context() as patch:
        patch.setattr("voice_workbench.desktop.draft_saves.atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        snapshot = queue.stage("work", {"title": "作品", "script": "正文"})
        assert queue.write_error == "disk full" and queue.pending["work"] == snapshot
    assert queue.stage("work", snapshot.body) == snapshot
    assert queue.write_error is None and DraftSaveQueue(queue.path).pending["work"] == snapshot


@pytest.fixture
def drafts(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    with TestClient(create_app(tmp_path, runner=ControlledRunner())) as client:
        window = Window(8793, tmp_path, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        errors = []
        monkeypatch.setattr(window, "show_error", errors.append)
        def immediate(action, success=None, failure=None):
            try:
                result = action(client)
            except Exception as exc:
                (failure or window.show_error)(str(exc))
            else:
                if success:
                    success(result)
        monkeypatch.setattr(window, "call", immediate)
        window.poll()
        window.start_creation("article")
        window.document_timer.stop()
        yield window, client, immediate, errors
        # Test the real close flush without contacting a user's running service.
        class LocalClient:
            def __init__(self, **_): pass
            def __enter__(self): return client
            def __exit__(self, *_): pass
        monkeypatch.setattr("voice_workbench.desktop.window.httpx.Client", LocalClient)
        window.close()


@pytest.mark.parametrize("navigate", ["new", "another"])
def test_edit_while_saving_then_navigate_keeps_latest_old_task(drafts, monkeypatch, navigate):
    window, client, immediate, _ = drafts
    target = window.selected_job
    requests = []
    monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: requests.append((action, success, failure)))
    window.text_edit.setPlainText("保存中的第一稿")
    window.save_document()
    window.text_edit.setPlainText("继续输入的第二稿")
    if navigate == "new":
        window.new_document()
        assert window.selected_job is None
    else:
        other = client.post("/jobs/draft", json={"title": "另一个任务", "script": "其他正文"}).json()
        window.render_jobs(client.get("/jobs").json())
        row = next(i for i in range(window.jobs_table.rowCount()) if window.jobs_table.item(i, 0).data(256) == other["id"])
        window.jobs_table.blockSignals(True)
        window.jobs_table.selectRow(row)
        window.jobs_table.blockSignals(False)
        window.select_job()
        assert window.selected_job == other["id"]
    assert window.draft_saves.pending[target].body["script"] == "继续输入的第二稿"
    action, success, _ = requests.pop(0)
    monkeypatch.setattr(window, "call", immediate)
    success(action(client))
    assert client.get(f"/jobs/{target}").json()["script"] == "继续输入的第二稿"
    assert target not in window.draft_saves.pending


def test_failed_generation_keeps_dirty_and_autosaves_without_resubmitting(drafts, monkeypatch):
    window, client, immediate, errors = drafts
    target = window.selected_job
    window.text_edit.setPlainText("提交失败后仍须保存")
    requests = []
    monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: requests.append((action, success, failure)))
    window.generate()
    assert len(requests) == 1 and window.submitting
    requests[0][2]("模拟连接失败")
    assert window.document_dirty and not window.submitting
    assert window.document_timer.isActive() and errors == ["模拟连接失败"]
    monkeypatch.setattr(window, "call", immediate)
    window.save_document()
    job = client.get(f"/jobs/{target}").json()
    assert job["script"] == "提交失败后仍须保存" and job["total"] == 0
    assert not window.document_dirty


def test_edits_after_generation_click_are_saved_after_response(drafts, monkeypatch):
    window, client, immediate, _ = drafts
    target = window.selected_job
    window.text_edit.setPlainText("用于这次生成")
    requests = []
    monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: requests.append((action, success, failure)))
    window.generate()
    window.text_edit.setPlainText("生成请求过程中继续编辑")
    action, success, _ = requests.pop()
    result = action(client)
    monkeypatch.setattr(window, "call", immediate)
    success(result)
    job = client.get(f"/jobs/{target}").json()
    assert job["script"] == "生成请求过程中继续编辑"
    assert job["segments"][0]["text"] == "用于这次生成"
    assert not window.document_dirty


def test_failed_save_is_restored_when_reopening_the_task(drafts, monkeypatch):
    window, client, _, _ = drafts
    target = window.selected_job
    window.text_edit.setPlainText("暂时未能提交的草稿")
    monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: failure("离线"))
    window.save_document()
    window.new_document()
    window.draft_saves = DraftSaveQueue(window.draft_saves.path)
    window.selected_job = target
    window.open_job_data(client.get(f"/jobs/{target}").json())
    assert window.text_edit.toPlainText() == "暂时未能提交的草稿"
    assert window.document_dirty and window.document_timer.isActive()


def test_draft_request_reads_widgets_before_worker_runs(drafts, monkeypatch):
    window, client, _, _ = drafts
    requests = []
    monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: requests.append((action, success)))
    window.start_creation("dialogue")
    action, _ = requests[-1]
    # Any later widget read by the network action would now fail.
    with monkeypatch.context() as patch:
        patch.setattr(window, "document_payload", lambda: pytest.fail("worker read Qt widgets"))
        job = action(client)
    assert job["editor_state"]["mode"] == "dialogue"
    requests[-1][1](job)


def test_close_during_new_task_request_waits_for_id_then_flushes_latest_text(drafts, monkeypatch, qtbot):
    window, client, immediate, _ = drafts
    requests = []
    monkeypatch.setattr(window, "call", lambda action, success=None, failure=None: requests.append((action, success)))
    window.start_creation("article")
    action, success = requests[-1]
    window.text_edit.setPlainText("创建请求还没返回时输入的正文")
    worker = object()
    window.workers.add(worker)
    window.show()
    window.close()
    assert window.isVisible() and window.close_requested and not window.closing
    job = action(client)
    monkeypatch.setattr(window, "call", immediate)
    success(job)
    window.workers.remove(worker)
    class LocalClient:
        def __init__(self, **_): pass
        def __enter__(self): return client
        def __exit__(self, *_): pass
    monkeypatch.setattr("voice_workbench.desktop.window.httpx.Client", LocalClient)
    qtbot.waitUntil(lambda: window.closing, timeout=2000)
    assert client.get(f"/jobs/{job['id']}").json()["script"] == "创建请求还没返回时输入的正文"
    assert not window.draft_saves.pending
