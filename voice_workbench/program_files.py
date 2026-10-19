"""Replace program files without moving or copying the user's data folders."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import tempfile

LEGACY_DOCUMENTS = ("README.md", "API.md", "UI_STYLE.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "requirements-release.txt",
    "docs/ARCHITECTURE.md", "docs/DIALOGUE_MODE_PLAN.md", "docs/MODEL_DISCOVERY.md", "docs/OPTIMIZATION.md",
    "docs/AUDIO_EDITING.md", "docs/APP_UPDATES.md", "docs/SPEECH_RECOGNITION.md", "docs/BATCH_INFERENCE.md", "docs/setup_qwen_batch.py",
    *(f"docs/images/{name}.png" for name in ("app", "workbench", "dialogue", "dialogue-history", "generation-list", "voice-library",
        "index-emotion", "workflow", "models", "engine-settings", "audio-manager", "model-discovery", "audio-editor", "app-updates")))


def retired_documents(candidate):
    """Only retire known old release files; personal files are never enumerated."""
    licenses = candidate / '_internal/legal/licenses'
    return (*LEGACY_DOCUMENTS, *(f'licenses/{path.relative_to(licenses).as_posix()}'
        for path in licenses.rglob('*') if path.is_file() and not path.is_symlink()))


def checked_child(path, root):
    resolved, base = path.resolve(), root.resolve()
    if (path.is_symlink() or resolved == base or not resolved.is_relative_to(base)
            or os.path.normcase(str(resolved)) != os.path.normcase(os.path.abspath(path))):
        raise RuntimeError(f"程序文件路径超出指定目录：{path}")
    return path


def ensure_unlocked(paths):
    """Check every existing file before writing, including DLLs used by the app."""
    if os.name != 'nt':
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = ctypes.c_void_p(-1).value
    for path in paths:
        handle = kernel.CreateFileW(str(path), 0x40000000, 0, None, 3, 0, None)
        if handle == invalid:
            error = ctypes.get_last_error()
            raise PermissionError(error, f"程序文件被占用或不可写，请关闭程序后重试：{path}")
        kernel.CloseHandle(handle)


def overwrite_program(candidate, target, documentation):
    """Only executables, bundled runtime and named public documents are managed."""
    allowed = {'声笺.exe', '声笺服务.exe', '_internal', *documentation}
    files, directories = {}, set()
    for path in candidate.rglob('*'):
        checked_child(path, candidate)
        relative = path.relative_to(candidate)
        if path.is_dir():
            if relative.parts[0] == '_internal' or any(Path(name).is_relative_to(relative) for name in documentation):
                directories.add(relative)
                continue
        elif path.is_file() and (relative.parts[0] in ('声笺.exe', '声笺服务.exe', '_internal') or relative.as_posix() in allowed):
            files[relative] = path
            continue
        raise RuntimeError(f"打包目录含非程序文件，拒绝覆盖：{relative}")
    old_files, old_directories = set(), set()
    internal = target/'_internal'
    if internal.exists():
        checked_child(internal, target)
        if not internal.is_dir():
            raise RuntimeError(f"程序目录不是文件夹：{internal}")
        old_directories.add(internal)
        for path in internal.rglob('*'):
            checked_child(path, target)
            (old_directories if path.is_dir() else old_files).add(path)
    for name in ('声笺.exe', '声笺服务.exe', *documentation):
        path = checked_child(target/name, target)
        if path.exists():
            if not path.is_file():
                raise RuntimeError(f"程序文件不是普通文件：{path}")
            old_files.add(path)
            for parent in path.parents:
                if parent == target:
                    break
                checked_child(parent, target)
                old_directories.add(parent)
    for relative in directories:
        destination = checked_child(target/relative, target)
        if destination.exists() and not destination.is_dir():
            raise RuntimeError(f"程序目录被文件占用：{destination}")
    for relative in files:
        destination = checked_child(target/relative, target)
        if destination.exists() and not destination.is_file():
            raise RuntimeError(f"程序文件被目录占用：{destination}")
    ensure_unlocked(sorted(old_files))
    target.mkdir(parents=True, exist_ok=True)
    for relative, source in files.items():
        destination = checked_child(target/relative, target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.声笺-update-', suffix='.tmp', dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    current = {target/relative for relative in files}
    for path in old_files - current:
        checked_child(path, target).unlink()
    for path in sorted(old_directories, key=lambda p: len(p.parts), reverse=True):
        checked_child(path, target)
        if not any(path.iterdir()):
            path.rmdir()
    return len(files)
