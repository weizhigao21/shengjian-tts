"""Identify — and only when safe, release — the process holding the service port.

The port is a published contract, not an implementation detail: ``mcp_server``
defaults to ``http://127.0.0.1:8765``, workflow links are configured against
it, and ``scripts/check_app_update.py`` asserts the service returns on the
original port after an update.  Moving to a free port therefore breaks
integrations this program cannot reconfigure, so a service left behind by a
crashed or killed session has to be cleared instead.

Clearing is deliberately narrow.  A process is terminated only when the
``service.json`` marker of *this* data directory names it and its executable
still matches, because the alternative — killing whatever owns the port — would
take down an unrelated program, or a second window serving a different data
directory that would then die mid-task.  Anything else is reported to the
caller and left running.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

IDENTITY_FILE = "service.json"
SHUTDOWN_TIMEOUT = 3.0
FORCE_TIMEOUT = 3.0
POLL_INTERVAL = 0.15

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

_KERNEL = None


@dataclass(frozen=True)
class Occupant:
    """Who holds the port, and whether this program may end it."""

    pid: int | None
    exe: str
    ours: bool
    data_dir: str = ""

    @property
    def label(self):
        name = Path(self.exe).name if self.exe else "未知程序"
        return f"{name}（PID {self.pid}）" if self.pid else name


@dataclass(frozen=True)
class Release:
    """Outcome of clearing the port: the occupant left behind, if any."""

    occupant: Occupant | None

    @property
    def cleared(self):
        return self.occupant is None


def identity_path(data_dir):
    return Path(data_dir) / IDENTITY_FILE


def write_identity(data_dir, port):
    """Record this process against its data directory so a crash can be undone."""
    path = identity_path(data_dir)
    # The real image path, not ``sys.executable``: a virtualenv launcher reports
    # ``.venv/Scripts/python.exe`` while the running image is the base interpreter,
    # and the mismatch would make our own leftover look like a recycled PID.
    image = executable_of(os.getpid()) or sys.executable
    try:
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "port": port,
            "data_dir": str(Path(data_dir).resolve()),
            "executable": image,
            "started_at": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Only the self-healing of the *next* launch depends on this file.
        return None
    return path


def clear_identity(data_dir, pid=None):
    """Drop the marker unless another process has already claimed it."""
    path = identity_path(data_dir)
    try:
        if pid is not None:
            recorded = json.loads(path.read_text(encoding="utf-8")).get("pid")
            if recorded != pid:
                return
        path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def read_identity(data_dir):
    try:
        return json.loads(identity_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def port_bindable(port):
    """A port can be refused while nothing listens, e.g. inside a WinNAT range."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "nt":
            # Without this, SO_REUSEADDR'd sockets report success over a live listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def listening_pids(port):
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                text=True, timeout=10, encoding="mbcs", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    pids = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        if fields[1].rsplit(":", 1)[-1] != str(port):
            continue
        if fields[4].isdigit():
            pids.add(int(fields[4]))
    return sorted(pids)


def executable_of(pid):
    if os.name != "nt":
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""
    import ctypes
    from ctypes import wintypes
    kernel = _kernel()
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


def alive(pid):
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes
    kernel = _kernel()
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def terminate(pid):
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True
    kernel = _kernel()
    handle = kernel.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel.TerminateProcess(handle, 1))
    finally:
        kernel.CloseHandle(handle)


def inspect(port, data_dir, service_exe=None):
    """Return the occupant of ``port``, or None when the port is free."""
    if port_bindable(port):
        return None
    pids = listening_pids(port)
    if not pids:
        # Refused by policy (Hyper-V / WinNAT reservation) rather than by a process.
        return Occupant(None, "", False)
    pid = pids[0]
    exe = executable_of(pid)
    ours, owner = _identify(pid, exe, data_dir, service_exe)
    return Occupant(pid, exe, ours, owner)


def release(port, data_dir, service_exe=None, log=None):
    """Free ``port`` when this data directory owns it; never touch anyone else."""
    note = log or (lambda message: None)
    occupant = inspect(port, data_dir, service_exe)
    if occupant is None or not occupant.ours:
        return Release(occupant)
    note(f"端口 {port} 上是上次残留的服务 {occupant.label}，正在关闭。")
    _request_shutdown(port)
    if _wait_released(port, SHUTDOWN_TIMEOUT):
        clear_identity(data_dir, occupant.pid)
        return Release(None)
    if terminate(occupant.pid):
        note(f"服务未响应关闭请求，已强制结束 {occupant.label}。")
    if _wait_released(port, FORCE_TIMEOUT):
        clear_identity(data_dir, occupant.pid)
        return Release(None)
    note(f"强行结束后端口 {port} 仍未释放。")
    return Release(occupant)


def _wait_released(port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_bindable(port):
            return True
        time.sleep(POLL_INTERVAL)
    return port_bindable(port)


def _request_shutdown(port):
    try:
        import httpx
        with httpx.Client(trust_env=False, timeout=2.0) as client:
            client.post(f"http://127.0.0.1:{port}/shutdown")
    except Exception:
        # An unreachable or wedged service is exactly what the forced end is for.
        pass


def _identify(pid, exe, data_dir, service_exe):
    """Decide whether ``pid`` is this data directory's own leftover service."""
    identity = read_identity(data_dir)
    if identity and identity.get("pid") == pid:
        recorded = identity.get("executable") or ""
        if not recorded or not exe or Path(recorded) == Path(exe):
            return True, str(data_dir)
        # The PID was recycled onto an unrelated executable.
        return False, ""
    if _frozen() and exe and service_exe and Path(exe) == Path(service_exe):
        # No marker: left by a build older than this one, or by hands that deleted it.
        # Only the packaged build can be identified this way; under source runs the
        # executable is a shared interpreter and every Python program would match.
        return True, ""
    return False, ""


def _frozen():
    return bool(getattr(sys, "frozen", False))


def _kernel():
    global _KERNEL
    if _KERNEL is None:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        _KERNEL = kernel
    return _KERNEL
