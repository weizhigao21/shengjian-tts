from pathlib import Path
import sys

import pytest

from scripts import build_package
from voice_workbench import runtime
from voice_workbench.errors import VoiceError
from voice_workbench.runner import check_voice


def test_packaged_processes_do_not_require_the_build_machine_python(tmp_path, monkeypatch):
    executable = tmp_path / "声笺.exe"
    executable.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    voice = {"backend": "windows_sapi", "python": "Z:/deleted-build-env/python.exe", "language": "zh"}
    check_voice(voice)
    assert runtime.project_root() == tmp_path
    assert runtime.service_command(8765, tmp_path / "data") == [str(tmp_path / "声笺服务.exe"), "serve", "--port", "8765", "--data-dir", str(tmp_path / "data")]
    assert runtime.engine_command(voice) == [str(tmp_path / "声笺服务.exe"), "engine"]
    with pytest.raises(VoiceError, match="Python 不存在"):
        check_voice({**voice, "backend": "index_tts"})
    external = tmp_path / "模型环境" / "python.exe"
    command = runtime.engine_command({**voice, "backend": "index_tts", "python": str(external)})
    assert command[:2] == [str(external), "-u"]
    assert Path(command[2]).name == "engine_worker.py"


def make_release(tmp_path, monkeypatch):
    monkeypatch.setattr(build_package, "ROOT", tmp_path)
    candidate = tmp_path / "build" / "staging" / "声笺"
    target = tmp_path / "dist" / "声笺"
    backup = tmp_path / "dist" / "声笺-backup-test"
    candidate.mkdir(parents=True)
    (candidate / "声笺.exe").write_text("new")
    (target / "data").mkdir(parents=True)
    (target / "声笺.exe").write_text("old")
    (target / "data" / "user-config.json").write_text("keep me")
    return candidate, target, backup


def test_rebuilding_keeps_existing_portable_data_and_old_program(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    build_package.publish(candidate, target, backup)
    assert (target / "声笺.exe").read_text() == "new"
    assert (target / "data" / "user-config.json").read_text() == "keep me"
    assert (backup / "声笺.exe").read_text() == "old"
    assert not (backup / "data").exists()


def test_failed_publish_restores_old_release_and_user_data(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    rename = Path.rename

    def fail_publish(path, destination):
        if path == candidate:
            raise PermissionError("文件被占用")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(PermissionError):
        build_package.publish(candidate, target, backup)
    assert (target / "声笺.exe").read_text() == "old"
    assert (target / "data" / "user-config.json").read_text() == "keep me"
    assert (candidate / "声笺.exe").read_text() == "new"


def test_publish_rejects_targets_outside_the_release_directory(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "important.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="路径超出"):
        build_package.publish(candidate, outside, backup)
    assert (outside / "important.txt").read_text() == "keep"
    assert (target / "data" / "user-config.json").is_file()
