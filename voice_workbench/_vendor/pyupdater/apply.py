"""Selected py-updater ZIP/process primitives; integration credit in SOURCE.md."""
from __future__ import annotations
import logging
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from .config import UpdateConfig
logger = logging.getLogger(__name__)


class ApplyError(RuntimeError):
    """更新应用失败；消息可直接展示给用户。"""


def _normalize(name: str) -> str:
    return name.replace("\\", "/")


def inspect_zip(zip_path: Path) -> list[str]:
    """校验更新包并返回文件条目名（不含目录项）。

    拒绝绝对路径、`..`、盘符（zip-slip 防御）——更新包内容会直接写进
    安装目录，一个越界条目就能覆盖系统文件。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise ApplyError(f"更新包不存在：{zip_path}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
    except zipfile.BadZipFile as exc:
        raise ApplyError(f"更新包已损坏：{exc}") from exc
    if not names:
        raise ApplyError("更新包是空的")
    for name in names:
        pure = PurePosixPath(_normalize(name))
        if pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
            raise ApplyError(f"更新包含非法路径：{name}")
    return names


def payload_prefix(names: list[str], cfg: UpdateConfig) -> str:
    """确定载荷在 zip 内的前缀（`''` 或 `MyApp-v1.2.3/`）。

    打包时可能把整个版本目录压进 zip（多一层目录），这里按"主程序所在
    层级"自动识别；找不到主程序直接报错，避免解压出一堆无用文件。
    """
    prefixes: list[str] = []
    for name in names:
        posix = _normalize(name)
        base = posix.rsplit("/", 1)[-1]
        if base == cfg.exe_name:
            prefixes.append(posix[: len(posix) - len(base)])
    if not prefixes:
        raise ApplyError(f"更新包里没有主程序：{cfg.exe_name}")
    return min(prefixes, key=len)


def _detached_flags() -> int:
    """分离进程标志（Windows）；非 Windows 平台返回 0。"""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def wait_for_process_exit(pid: int, timeout: float = 60.0) -> bool:
    """等待指定进程退出，返回是否已退出（超时返回 False）。

    Windows 上用 `OpenProcess + WaitForSingleObject`（不引入 psutil）；
    其他平台退回轮询。pid 为 0 或进程不存在视为已退出。
    """
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        from ctypes import wintypes
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return True
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.3)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def launch_updater(
    install_dir: Path,
    zip_path: Path,
    cfg: UpdateConfig,
    pid: int | None = None,
    work_dir: Path | None = None,
    environment: dict | None = None,
) -> subprocess.Popen:
    """把更新程序复制到缓存目录后启动（主程序下载完成后调用）。

    **必须是副本**：更新程序自己也在安装目录里，原地替换时会被 Windows
    锁定；复制到 %TEMP% 再运行才能顺利替换整个目录。同时把配置文件路径
    显式传给更新程序——它运行在临时目录，按自身位置找不到配置。
    """
    install_dir = Path(install_dir)
    source = install_dir / cfg.updater_name
    if not source.is_file():
        raise ApplyError(
            f"安装目录里没有 {cfg.updater_name}，无法自动更新；请到发布页手动下载完整包"
        )
    work_dir = Path(work_dir) if work_dir else Path(zip_path).parent
    work_dir.mkdir(parents=True, exist_ok=True)
    runner = work_dir / cfg.updater_name
    if runner.exists():
        runner.unlink(missing_ok=True)
    shutil.copy2(source, runner)

    args = [
        str(runner),
        "--pid", str(pid if pid is not None else os.getpid()),
        "--zip", str(Path(zip_path)),
        "--dir", str(install_dir),
    ]
    if cfg.config_path is not None:
        args += ["--config", str(cfg.config_path)]
    return subprocess.Popen(
        args, cwd=str(work_dir), creationflags=_detached_flags(), close_fds=True,
        env=dict(environment if environment is not None else os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    )
