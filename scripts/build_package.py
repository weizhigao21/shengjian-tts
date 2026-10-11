"""Build into a fresh directory, validate, then publish with reversible backups."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def contained(path, parent):
    resolved = path.resolve()
    base = parent.resolve()
    if not base.is_relative_to(ROOT.resolve()) or resolved == base or not resolved.is_relative_to(base):
        raise RuntimeError(f"路径超出指定目录，停止操作：{path}")
    return path


def publish(candidate, target, backup):
    """Keep portable data at its original public path without copying model weights."""
    contained(candidate, ROOT / "build")
    for path in (target, backup):
        contained(path, ROOT / "dist")
    for path in (target / "data", backup / "data", candidate / "data"):
        contained(path, path.parent)
    moved_old = False
    moved_data = False
    try:
        if target.exists():
            target.rename(backup)
            moved_old = True
            if (backup / "data").exists():
                (backup / "data").rename(candidate / "data")
                moved_data = True
        candidate.rename(target)
    except Exception:
        if moved_data and (candidate / "data").exists():
            (candidate / "data").rename(backup / "data")
        if moved_old:
            backup.rename(target)
        raise
    if moved_old:
        print(f"旧程序备份：{backup}", flush=True)
        if moved_data:
            print("已有 data 已延续到新发布目录；旧程序备份不重复保存 data。", flush=True)


def main():
    if os.name != "nt":
        raise RuntimeError("此脚本用于 Windows 打包。")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    work = contained(ROOT / "build" / ("package-" + stamp), ROOT / "build")
    work.mkdir(parents=True, exist_ok=False)
    windows = Path(os.environ.get("WINDIR", "C:/Windows"))
    interpreter = Path(sys.executable).resolve().parent
    # Qt can use Windows' ICU. A different ICU on PATH has incompatible exports.
    build_path = os.pathsep.join(str(p) for p in (interpreter, interpreter / "Scripts",
                                windows / "System32", windows, windows / "System32/WindowsPowerShell/v1.0"))
    environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "cache"), PATH=build_path)
    print("[4/5] 正在打包，详细日志如下…", flush=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "声笺.spec"),
                    "--distpath", str(work / "dist"), "--workpath", str(work / "work"),
                    "--noconfirm"], cwd=ROOT, env=environment, check=True)
    candidate = work / "dist" / "声笺"
    for name in ("声笺.exe", "声笺服务.exe", "_internal/voice_workbench/assets/app.ico",
                 "_internal/voice_workbench/assets/model_catalog.json", "_internal/voice_workbench/engine_worker.py"):
        if not (candidate / name).is_file():
            raise RuntimeError(f"产物不完整：{candidate / name}")
    result = subprocess.run([str(candidate / "声笺服务.exe"), "--version"], capture_output=True,
                            text=True, encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"), check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    print("[5/5] 产物入口验证：" + result.stdout.strip(), flush=True)
    for name in ("README.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, candidate / name)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / "声笺"
    backup = dist / ("声笺-backup-" + stamp)
    publish(candidate, target, backup)
    print(f"打包完成：{target / '声笺.exe'}", flush=True)
    print("分发时请压缩整个声笺文件夹。模型和外部引擎不包含在包内。", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[错误] 打包失败：{exc}\n请查看上方日志；若旧程序正在运行，请关闭后再打包。", file=sys.stderr)
        raise SystemExit(1)
