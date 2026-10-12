"""Exercise the Windows batch entry with a real environment lacking pip."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import venv

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows batch entry")
ROOT = Path(__file__).resolve().parents[1]


def batch_project(tmp_path):
    environment = tmp_path / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    shutil.copy2(ROOT / "打包.bat", tmp_path / "打包.bat")
    # Runtime modules are placeholders: this test only exercises the bootstrap,
    # without downloading packages or rebuilding a release.
    for module in ("fastapi", "uvicorn", "PyQt6", "httpx"):
        (tmp_path / (module + ".py")).write_text("", encoding="utf-8")
    metadata = environment / "Lib/site-packages/pyinstaller-6.20.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: pyinstaller\nVersion: 6.20.0\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/build_package.py").write_text(
        "import json, sys, pip\nfrom pathlib import Path\n"
        "Path('build-reached.json').write_text(json.dumps({'python': sys.executable, 'pip': pip.__version__}))\n",
        encoding="utf-8")
    return environment / "Scripts/python.exe"


def run_batch(tmp_path):
    return subprocess.run(["cmd.exe", "/d", "/c", "打包.bat", "--no-pause"], cwd=tmp_path,
        env=dict(os.environ, PIP_NO_INDEX="1"), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)


def test_batch_repairs_missing_pip_offline_before_building(tmp_path):
    python = batch_project(tmp_path)
    absent = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True)
    assert absent.returncode != 0
    result = run_batch(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "build-reached.json").read_text())
    assert Path(report["python"]) == python and report["pip"]
    assert "当前环境缺少 pip，正在自动修复" in result.stdout
    assert "运行依赖已就绪" in result.stdout and "PyInstaller 已就绪" in result.stdout
    # Subsequent runs use the repaired environment without reinstalling pip.
    repeated = run_batch(tmp_path)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert "正在自动修复" not in repeated.stdout


def test_batch_explains_failed_pip_bootstrap_and_never_reaches_build(tmp_path):
    batch_project(tmp_path)
    (tmp_path / "ensurepip.py").write_text("raise RuntimeError('ensurepip unavailable')\n", encoding="utf-8")
    result = run_batch(tmp_path)
    assert result.returncode == 1
    assert "无法自动安装 pip" in result.stdout
    assert "确保包含 pip 和 ensurepip" in result.stdout
    assert not (tmp_path / "build-reached.json").exists()
