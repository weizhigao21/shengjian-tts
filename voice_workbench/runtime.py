"""Source and packaged process paths; model Python environments stay external."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
import threading

_dll_lock = threading.RLock()


def frozen():
    return bool(getattr(sys, "frozen", False))


def project_root():
    return Path(sys.executable).resolve().parent if frozen() else resource_root()


def resource_root():
    return Path(__file__).resolve().parent.parent


def service_command(port, data_dir):
    prefix = [str(project_root() / "声笺服务.exe")] if frozen() else [sys.executable, "-u", "-m", "voice_workbench"]
    return prefix + ["serve", "--port", str(port), "--data-dir", str(data_dir)]


def audio_decode_command(source, target):
    prefix = [str(project_root() / "声笺服务.exe")] if frozen() else [sys.executable, "-u", "-m", "voice_workbench"]
    return prefix + ["decode-audio", str(source), str(target)]


def engine_command(voice):
    if frozen() and voice["backend"] == "windows_sapi":
        return [str(project_root() / "声笺服务.exe"), "engine"]
    return [voice["python"], "-u", str(resource_root() / "voice_workbench" / "engine_worker.py")]


@contextmanager
def external_environment(environment=None):
    """Don't give external Python/CUDA programs the bundle's DLL search paths."""
    env = dict(os.environ if environment is None else environment)
    if not frozen() or sys.platform != "win32":
        yield env
        return
    import ctypes
    bundle = resource_root()
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                 if p and not Path(p).resolve().is_relative_to(bundle))
    with _dll_lock:
        kernel = ctypes.windll.kernel32
        saved = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(saved), saved)
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError()
        try:
            yield env
        finally:
            kernel.SetDllDirectoryW(saved.value or None)
