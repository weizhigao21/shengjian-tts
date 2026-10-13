"""Build in isolation, validate, then overwrite program files and retain user data."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
# Build from this checkout, even when an older wheel is installed globally.
sys.path.insert(0, str(ROOT))
from voice_workbench.app_updates.manifest import write_manifest, digest_file, MANIFEST, UPDATER

if __package__:
    from .package_files import overwrite_program
else:
    from package_files import overwrite_program

DOCUMENTATION = ("README.md", "API.md", "UI_STYLE.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "requirements-release.txt")
DOCUMENTATION_GUIDES = ("docs/ARCHITECTURE.md", "docs/DIALOGUE_MODE_PLAN.md", "docs/MODEL_DISCOVERY.md",
                        "docs/OPTIMIZATION.md", "docs/AUDIO_EDITING.md", "docs/APP_UPDATES.md")
DOCUMENTATION_LICENSES = tuple(path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "licenses").rglob("*"))
                              if path.is_file() and not path.is_symlink())
DOCUMENTATION_ASSETS = tuple(f"docs/images/{name}.png" for name in (
    "app", "workbench", "dialogue", "dialogue-history", "generation-list", "voice-library", "index-emotion", "workflow", "models", "engine-settings", "audio-manager", "model-discovery", "audio-editor", "app-updates"))


def copy_documentation(candidate):
    """Keep relative README images working in the unpacked program folder."""
    for name in (*DOCUMENTATION, *DOCUMENTATION_GUIDES, *DOCUMENTATION_ASSETS, *DOCUMENTATION_LICENSES):
        destination = candidate / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)


def contained(path, parent):
    resolved = path.resolve()
    base = parent.resolve()
    if not base.is_relative_to(ROOT.resolve()) or resolved == base or not resolved.is_relative_to(base):
        raise RuntimeError(f"路径超出指定目录，停止操作：{path}")
    return path


def publish(candidate, target):
    """Publish only program files; existing configuration/data folders stay in place."""
    contained(candidate, ROOT / "build")
    contained(target, ROOT / "dist")
    overwrite_program(candidate, target, (UPDATER, MANIFEST, *DOCUMENTATION, *DOCUMENTATION_GUIDES, *DOCUMENTATION_ASSETS, *DOCUMENTATION_LICENSES))


def distribution_archive(candidate, destination):
    """Publish program files only, even if a locally used folder contains user data."""
    contained(destination, ROOT / "dist")
    allowed = {"声笺.exe", "声笺服务.exe", UPDATER, MANIFEST, "_internal", *DOCUMENTATION}
    temporary = destination.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(candidate.rglob('*')):
                relative = path.relative_to(candidate)
                included = relative.parts[0] in allowed or relative.as_posix() in (*DOCUMENTATION_GUIDES, *DOCUMENTATION_ASSETS, *DOCUMENTATION_LICENSES)
                if path.is_file() and not path.is_symlink() and included:
                    archive.write(path, str(Path('声笺') / relative))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


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
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "声笺更新器.spec"),
                    "--distpath", str(work / "dist"), "--workpath", str(work / "updater-work"),
                    "--noconfirm"], cwd=ROOT, env=environment, check=True)
    candidate = work / "dist" / "声笺"
    shutil.copy2(work / "dist" / UPDATER, candidate / UPDATER)
    for name in ("声笺.exe", "声笺服务.exe", "_internal/voice_workbench/assets/app.ico",
                 "_internal/voice_workbench/assets/model_catalog.json", "_internal/voice_workbench/engine_worker.py",
                 "_internal/voice_workbench/index_emotion.py", "_internal/PyQt6/Qt6/translations/qtbase_zh_CN.qm"):
        if not (candidate / name).is_file():
            raise RuntimeError(f"产物不完整：{candidate / name}")
    result = subprocess.run([str(candidate / "声笺服务.exe"), "--version"], capture_output=True,
                            text=True, encoding="utf-8", env=dict(os.environ, PYTHONUTF8="1"), check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    print("[5/5] 产物入口验证：" + result.stdout.strip(), flush=True)
    copy_documentation(candidate)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    version = result.stdout.strip().rsplit(' ', 1)[-1]
    write_manifest(candidate, version)
    archive = distribution_archive(candidate, dist / f"shengjian-v{version}-windows-x64.zip")
    (dist / 'SHA256SUMS.txt').write_text(f"{digest_file(archive)}  {archive.name}\n", encoding='ascii')
    target = dist / "声笺"
    publish(candidate, target)
    contained(work, ROOT / "build")
    shutil.rmtree(work)
    print(f"打包完成：{target / '声笺.exe'}", flush=True)
    print(f"分发包：{archive}（只含程序文件，不含数据库、音色与生成音频）", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[错误] 打包失败：{exc}\n请查看上方日志；若旧程序正在运行，请关闭后再打包。", file=sys.stderr)
        raise SystemExit(1)
