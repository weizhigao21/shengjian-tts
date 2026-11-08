"""The port is a published contract, so only this data directory's own leftover may be cleared.

Every test starts a real listener: the point of the module is telling one
process apart from another, which a fake would not exercise.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from voice_workbench.services import port_guard

LISTENER = """
import socket, sys, time
sock = socket.socket()
sock.bind(("127.0.0.1", int(sys.argv[1])))
sock.listen()
time.sleep(60)
"""


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_listening(port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_guard.port_bindable(port):
            return
        time.sleep(0.05)
    raise AssertionError(f"端口 {port} 没有被监听")


def listening_pid(port):
    """The PID that actually holds the socket.

    ``.venv/Scripts/python.exe`` is a launcher that spawns the real interpreter,
    so ``Popen.pid`` is not the process netstat reports.  The shipped marker is
    written from inside the service, so production is unaffected — only this
    test needs the real holder.
    """
    pids = port_guard.listening_pids(port)
    if not pids:
        pytest.skip("该平台无法枚举端口持有者")
    return pids[0]


def claim(port, tmp_path, pid, executable=None):
    (tmp_path / "service.json").write_text(json.dumps({
        "pid": pid,
        "port": port,
        "data_dir": str(Path(tmp_path).resolve()),
        "executable": executable or port_guard.executable_of(pid),
        "started_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def listener():
    port = free_port()
    process = subprocess.Popen([sys.executable, "-c", LISTENER, str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_listening(port)
        yield port, process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def test_a_free_port_reports_no_occupant(tmp_path):
    assert port_guard.inspect(free_port(), tmp_path, sys.executable) is None


def test_a_foreign_listener_is_identified_but_never_killed(listener, tmp_path):
    port, process = listener
    pid = listening_pid(port)

    occupant = port_guard.inspect(port, tmp_path, sys.executable)

    assert occupant.pid == pid
    assert occupant.ours is False
    assert "python" in occupant.exe.casefold()

    result = port_guard.release(port, tmp_path, sys.executable)
    assert result.cleared is False
    assert port_guard.alive(pid), "别人的进程不能被碰"
    assert port_guard.port_bindable(port) is False


def test_our_own_leftover_is_shut_down_and_the_marker_dropped(listener, tmp_path):
    port, process = listener
    claim(port, tmp_path, listening_pid(port))

    result = port_guard.release(port, tmp_path, sys.executable)

    assert result.cleared is True
    assert port_guard.port_bindable(port) is True
    assert process.wait(timeout=10) is not None
    assert not port_guard.identity_path(tmp_path).exists()


def test_a_recycled_pid_is_not_mistaken_for_our_service(listener, tmp_path):
    """The recorded PID now belongs to an unrelated executable."""
    port, _ = listener
    pid = listening_pid(port)
    claim(port, tmp_path, pid, executable=str(tmp_path / "声笺服务.exe"))

    occupant = port_guard.inspect(port, tmp_path, sys.executable)

    assert occupant.ours is False
    assert occupant.pid == pid


def test_the_marker_records_the_real_image_not_the_venv_launcher(tmp_path):
    """``sys.executable`` names the venv shim while the image is the base interpreter.

    Storing the shim would make every source-run leftover look like a recycled
    PID, so nothing would ever be cleared.
    """
    port_guard.write_identity(tmp_path, 8765)

    marker = port_guard.read_identity(tmp_path)

    assert marker["pid"] == os.getpid()
    assert marker["executable"] == port_guard.executable_of(os.getpid())


def test_the_marker_of_a_newer_instance_is_not_deleted(tmp_path):
    port_guard.write_identity(tmp_path, 8765)

    port_guard.clear_identity(tmp_path, pid=-1)
    assert port_guard.identity_path(tmp_path).exists()

    port_guard.clear_identity(tmp_path)
    assert not port_guard.identity_path(tmp_path).exists()


def test_a_stale_marker_does_not_block_a_free_port(tmp_path):
    """A marker left by a process that already exited must not block the launch."""
    port = free_port()
    claim(port, tmp_path, pid=1)

    assert port_guard.release(port, tmp_path, sys.executable).cleared is True


def test_service_executable_matches_the_launch_command():
    from voice_workbench.runtime import service_command, service_executable

    assert service_command(8765, Path("data"))[0] == service_executable()
