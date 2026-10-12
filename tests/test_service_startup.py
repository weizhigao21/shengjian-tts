from types import SimpleNamespace

import pytest

from voice_workbench.gui import Window
from voice_workbench.service_status import startup_failure


@pytest.mark.parametrize("output,title", [
    ("ERROR: [Errno 13] error while attempting to bind on address ('127.0.0.1', 8765): 权限不允许", "端口 8765 被系统拒绝"),
    ("ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8765): address already in use", "端口 8765 已被占用"),
    ("ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8765): address already in use", "端口 8765 已被占用"),
    ("RuntimeError: 此数据目录已有语音服务运行。", "该数据目录已有服务"),
    ("PermissionError: [Errno 13] Permission denied: 'data/service.lock'", "数据目录访问被拒绝"),
    ("ModuleNotFoundError: No module named 'missing'", "服务启动失败 · 退出码 1"),
])
def test_service_failure_identifies_its_cause_and_keeps_error_detail(output, title):
    actual, detail = startup_failure(output, 8765, 1)
    assert actual == title
    assert output in detail


def window_without_service(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(Window, "load_saved_jobs", lambda self: None)
    window = Window(8765, tmp_path, autostart=False)
    qtbot.addWidget(window)
    for timer in (window.timer, window.settings.timer, window.document_timer):
        timer.stop()
    return window


def test_reconnect_checks_once_and_never_duplicates_a_running_service(tmp_path, qtbot, monkeypatch):
    window = window_without_service(tmp_path, qtbot, monkeypatch)
    calls = []
    monkeypatch.setattr(window, "call", lambda *args: calls.append(args))
    window.reconnect_service()
    window.reconnect_service()
    assert len(calls) == 1
    assert window.connecting_service and not window.reconnect_button.isEnabled()
    window.connecting_service = False
    window.owns_service = True
    window.process = SimpleNamespace(poll=lambda: None)
    polls = []
    monkeypatch.setattr(window, "poll", lambda: polls.append(True))
    window.reconnect_service()
    assert polls == [True] and len(calls) == 1
    window.owns_service = False
    window.close()


def test_restart_diagnoses_only_new_log_and_late_poll_cannot_replace_it(tmp_path, qtbot, monkeypatch):
    window = window_without_service(tmp_path, qtbot, monkeypatch)
    log = tmp_path / "logs/service.log"
    log.parent.mkdir()
    log.write_text("PermissionError: OLD unrelated failure\n", encoding="utf-8")
    old_handle = log.open("ab")
    window.process_log = old_handle
    process = SimpleNamespace(poll=lambda: 1, returncode=1)
    def spawn(*args, **kwargs):
        assert args[0][-4:] == ['--port', '8765', '--data-dir', str(tmp_path)]
        kwargs['stdout'].write(b"ERROR: [Errno 13] error while attempting to bind on address ('127.0.0.1', 8765): Permission denied\n")
        kwargs['stdout'].flush()
        return process
    monkeypatch.setattr("voice_workbench.gui.subprocess.Popen", spawn)
    monkeypatch.setattr("voice_workbench.gui.QTimer.singleShot", lambda *args: None)
    window._start_service("Connection refused")
    assert old_handle.closed and window.port == 8765
    assert window.reconnect_button.isEnabled()
    window.poll()
    assert window.banner.text() == "端口 8765 被系统拒绝"
    assert "OLD unrelated" not in window.service_output_text
    assert "Permission denied" in window.banner.toolTip()
    # An earlier in-flight health check may finish after the process has exited.
    window.process = None
    calls = []
    monkeypatch.setattr(window, "call", lambda *args: calls.append(args))
    window.poll()
    window.process = process
    calls[0][2]("Connection refused")
    assert window.banner.text() == "端口 8765 被系统拒绝"
    assert not window.polling and not window.ready
    window.close()


def test_failed_process_launch_does_not_keep_the_old_process(tmp_path, qtbot, monkeypatch):
    window = window_without_service(tmp_path, qtbot, monkeypatch)
    window.process = SimpleNamespace(poll=lambda: 1, returncode=1)
    window.owns_service = True
    def denied(*args, **kwargs):
        raise PermissionError("Executable access denied")
    monkeypatch.setattr("voice_workbench.gui.subprocess.Popen", denied)
    window._start_service("Connection refused")
    assert window.process is None and not window.owns_service
    assert not window.connecting_service and window.reconnect_button.isEnabled()
    assert "Executable access denied" in window.banner.toolTip()
    window.poll()
    assert "Executable access denied" in window.banner.toolTip()
    window.close()


def test_reconnect_can_adopt_an_existing_service_after_its_own_process_exits(tmp_path, qtbot, monkeypatch):
    window = window_without_service(tmp_path, qtbot, monkeypatch)
    window.process = SimpleNamespace(poll=lambda: 1, returncode=1)
    window.owns_service = True
    window.service_launch_error = "old failure"
    calls = []
    monkeypatch.setattr(window, "call", lambda *args: calls.append(args))
    window.reconnect_service()
    assert window.process is None and not window.owns_service
    assert window.service_launch_error is None and len(calls) == 1
    window.close()
