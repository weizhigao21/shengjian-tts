from pathlib import Path
import sys
import zipfile

import pytest

from scripts import build_package, package_files
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


def test_rebuilding_overwrites_program_without_backups_and_keeps_portable_data(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    original = (target / "data").stat()
    build_package.publish(candidate, target)
    assert (target / "声笺.exe").read_text() == "new"
    assert (target / "data" / "user-config.json").read_text() == "keep me"
    assert not backup.exists() and not list(target.parent.glob('声笺-backup-*'))
    assert (target / "data").stat().st_ino == original.st_ino
    assert (candidate / "声笺.exe").read_text() == "new"


def test_locked_program_fails_before_overwriting_any_file_or_user_data(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    def fail_publish(paths):
        assert target/'声笺.exe' in paths
        raise PermissionError("文件被占用")
    monkeypatch.setattr(package_files, "ensure_unlocked", fail_publish)
    with pytest.raises(PermissionError):
        build_package.publish(candidate, target)
    assert (target / "声笺.exe").read_text() == "old"
    assert (target / "data" / "user-config.json").read_text() == "keep me"
    assert (candidate / "声笺.exe").read_text() == "new"
    assert not backup.exists()


def test_publish_rejects_targets_outside_the_release_directory(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "important.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="路径超出"):
        build_package.publish(candidate, outside)
    assert (outside / "important.txt").read_text() == "keep"
    assert (target / "data" / "user-config.json").is_file()


def test_overwrite_replaces_runtime_prunes_stale_libraries_and_preserves_extra_folders(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    for directory in ('config', 'configs', 'models', 'audio', 'docs'):
        (target/directory).mkdir()
        (target/directory/'personal.txt').write_bytes(b'private')
    (candidate/'_internal').mkdir()
    (candidate/'_internal/library.dll').write_bytes(b'new runtime')
    (target/'_internal').mkdir()
    (target/'_internal/library.dll').write_bytes(b'old runtime')
    (target/'_internal/obsolete.dll').write_bytes(b'obsolete')
    (candidate/'README.md').write_bytes(b'new documentation')
    (target/'README.md').write_bytes(b'old documentation')
    build_package.publish(candidate, target)
    assert (target/'_internal/library.dll').read_bytes() == b'new runtime'
    assert not (target/'_internal/obsolete.dll').exists()
    assert (target/'README.md').read_bytes() == b'new documentation'
    assert all((target/directory/'personal.txt').read_bytes() == b'private'
        for directory in ('config', 'configs', 'models', 'audio', 'docs'))
    assert not list(target.rglob('.声笺-update-*')) and not backup.exists()


@pytest.mark.parametrize('relative', ['data/config.json', 'config/settings.json', 'docs/private.txt'])
def test_candidate_cannot_overwrite_config_or_unmanaged_files(tmp_path, monkeypatch, relative):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    path = candidate/relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'not program data')
    with pytest.raises(RuntimeError, match='非程序文件'):
        build_package.publish(candidate, target)
    assert (target/'声笺.exe').read_text() == 'old'
    assert (target/'data/user-config.json').read_text() == 'keep me'


def test_failed_copy_keeps_original_file_and_removes_temporary_file(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    def fail_copy(*args):
        raise OSError('copy failed')
    monkeypatch.setattr(package_files.shutil, 'copy2', fail_copy)
    with pytest.raises(OSError, match='copy failed'):
        build_package.publish(candidate, target)
    assert (target/'声笺.exe').read_text() == 'old'
    assert (target/'data/user-config.json').read_text() == 'keep me'
    assert not list(target.rglob('.声笺-update-*'))


def test_distribution_zip_excludes_personal_data_even_in_used_release(tmp_path, monkeypatch):
    candidate, target, backup = make_release(tmp_path, monkeypatch)
    (target / '_internal').mkdir()
    (target / '_internal' / 'library.dll').write_bytes(b'library')
    (target / 'data' / 'tasks.sqlite3').write_bytes(b'personal database')
    (target / 'private.txt').write_text('private')
    (target / 'README.md').write_text('documentation')
    (target / 'API.md').write_text('API documentation')
    (target / 'UI_STYLE.md').write_text('Reusable UI style')
    (target / 'docs' / 'images').mkdir(parents=True)
    (target / 'docs' / 'images' / 'workbench.png').write_bytes(b'public screenshot')
    (target / 'docs' / 'images' / 'app.png').write_bytes(b'public icon')
    (target / 'docs' / 'images' / 'personal.png').write_bytes(b'private screenshot')
    (target / 'docs' / 'personal.txt').write_text('private notes')
    for name in build_package.DOCUMENTATION_GUIDES:
        (target / name).write_text('public design documentation')
    archive = build_package.distribution_archive(target, tmp_path/'dist/release.zip')
    with zipfile.ZipFile(archive) as saved:
        assert set(saved.namelist()) == {'声笺/声笺.exe', '声笺/_internal/library.dll',
                                       '声笺/README.md', '声笺/API.md', '声笺/UI_STYLE.md',
                                       '声笺/docs/images/workbench.png', '声笺/docs/images/app.png',
                                       *('声笺/'+name for name in build_package.DOCUMENTATION_GUIDES)}
        assert saved.read('声笺/_internal/library.dll') == b'library'
        assert saved.read('声笺/docs/images/workbench.png') == b'public screenshot'
    assert (target / 'data' / 'tasks.sqlite3').read_bytes() == b'personal database'


def test_copy_documentation_keeps_all_relative_readme_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(build_package, "ROOT", tmp_path)
    for name in (*build_package.DOCUMENTATION, *build_package.DOCUMENTATION_GUIDES, *build_package.DOCUMENTATION_ASSETS):
        source = tmp_path / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(name.encode("utf-8"))
    candidate = tmp_path / "build" / "staging" / "声笺"
    build_package.copy_documentation(candidate)
    for name in (*build_package.DOCUMENTATION, *build_package.DOCUMENTATION_GUIDES, *build_package.DOCUMENTATION_ASSETS):
        assert (candidate / name).read_bytes() == (tmp_path / name).read_bytes()
