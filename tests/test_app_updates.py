import hashlib
import json
import stat
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from voice_workbench.app_updates import client, manifest, apply
from voice_workbench._vendor.pyupdater.apply import ApplyError


def release_data(payload=b'zip', version='0.8.0'):
    name = f'shengjian-v{version}-windows-x64.zip'
    asset = {'name': name, 'size': len(payload), 'state': 'uploaded',
             'browser_download_url': f'https://github.com/{client.SLUG}/releases/download/v{version}/{name}',
             'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}
    return {'tag_name': 'v' + version, 'body': '<b>plain release notes</b>', 'assets': [asset]}


def http_factory(handler):
    def factory(**kwargs):
        # Proxy mounts override MockTransport; keep every retry isolated.
        kwargs.pop('proxy', None)
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return factory


def make_bundle(tmp_path, extra=None, version='0.8.0'):
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    for name in manifest.EXECUTABLES:
        (candidate / name).write_bytes(b'MZ-new-' + name.encode())
    (candidate / '_internal').mkdir()
    (candidate / '_internal' / 'new.dll').write_bytes(b'new-runtime')
    (candidate / 'README.md').write_text('new readme')
    manifest.write_manifest(candidate, version)
    archive = tmp_path / 'update.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        for path in candidate.rglob('*'):
            if path.is_file():
                zf.write(path, '声笺/' + path.relative_to(candidate).as_posix())
        for name, value in (extra or {}).items():
            zf.writestr(name, value)
    return archive


def rewrite_zip(path, replace=None, omit=None):
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i) for i in zf.infolist() if i.filename not in (omit or ())}
    contents.update(replace or {})
    with zipfile.ZipFile(path, 'w') as zf:
        for name, value in contents.items():
            zf.writestr(name, value)


def prepared_job(tmp_path):
    archive = make_bundle(tmp_path)
    install = tmp_path / 'installed'
    install.mkdir()
    (install / '声笺.exe').write_bytes(b'MZ-old')
    (install / '_internal').mkdir()
    (install / '_internal' / 'stale.dll').write_bytes(b'old')
    (install / 'data').mkdir()
    (install / 'data' / 'config.json').write_bytes(b'personal config')
    cfg = client.configuration()
    cfg.save(tmp_path / 'updater-config.json')
    job = {'install': str(install), 'archive': str(archive), 'sha256': manifest.digest_file(archive),
           'version': '0.8.0', 'data_dir': str(install / 'data'), 'port': 8877, 'service_pid': 0}
    (tmp_path / 'job.json').write_text(json.dumps(job), encoding='utf-8')
    args = SimpleNamespace(config=str(tmp_path / 'updater-config.json'), dir=str(install),
                           zip=str(archive), pid=0, no_restart=True)
    return args, cfg, job


def test_check_selects_exact_windows_package_and_preserves_plain_notes():
    data = release_data()
    data['assets'].insert(0, {'name': 'shengjian-v0.8.0-source.zip'})
    result = client.check_release(http_factory(lambda _: httpx.Response(200, json=data)))
    assert result.filename == 'shengjian-v0.8.0-windows-x64.zip'
    assert result.notes == '<b>plain release notes</b>'
    assert result.sha256 == hashlib.sha256(b'zip').hexdigest()


@pytest.mark.parametrize('change', [
    {'draft': True}, {'prerelease': True}, {'tag_name': 'v0.8.0-rc1'}, {'assets': []},
    {'tag_name': '../latest'}, {'tag_name': 'v0.8'},
])
def test_unusable_release_is_not_reported_as_up_to_date(change):
    data = release_data() | change
    with pytest.raises(client.UpdateError):
        client.check_release(http_factory(lambda _: httpx.Response(200, json=data)))


@pytest.mark.parametrize('field,value', [('size', 0), ('size', True), ('size', client.MAX_DOWNLOAD + 1),
                                       ('digest', 'sha256:wrong'), ('browser_download_url', 'https://example.com/malicious.zip')])
def test_release_metadata_must_be_bounded_and_trusted(field, value):
    data = release_data()
    data['assets'][0][field] = value
    with pytest.raises(client.UpdateError):
        client.parse_release(data, None)


def test_ambiguous_asset_rejected():
    data = release_data()
    data['assets'] *= 2
    with pytest.raises(client.UpdateError, match='完整包'):
        client.parse_release(data, None)


def test_checksum_file_fallback_matches_filename_exactly():
    data = release_data()
    data['assets'][0]['digest'] = None
    data['assets'].append({'name': 'SHA256SUMS.txt',
                          'browser_download_url': f'https://github.com/{client.SLUG}/releases/download/v0.8.0/SHA256SUMS.txt'})
    content = '0' * 64 + '  source.zip\n' + hashlib.sha256(b'zip').hexdigest() + '  ' + data['assets'][0]['name']
    def handler(request):
        return httpx.Response(200, text=content) if request.url.path.endswith('.txt') else httpx.Response(200, json=data)
    result = client.check_release(http_factory(handler))
    assert result.sha256 == hashlib.sha256(b'zip').hexdigest()


@pytest.mark.parametrize('code,headers,message', [(404, {}, '没有'), (429, {}, '频繁'),
                                                (403, {'x-ratelimit-remaining': '0'}, '频繁'), (500, {}, '无法检查')])
def test_api_failure_reports_error(code, headers, message):
    with pytest.raises(client.UpdateError, match=message):
        client.check_release(http_factory(lambda _: httpx.Response(code, headers=headers)))


def test_verified_download_progress_and_final_name(tmp_path):
    payload = b'ZIP' * 200000
    release = client.parse_release(release_data(payload), None)
    progress = []
    result = client.download(release, tmp_path, progress=lambda *v: progress.append(v),
                             client_factory=http_factory(lambda _: httpx.Response(200, content=payload)))
    assert result.read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('received', [b'zap', b'zi', b'zip-extra'])
def test_bad_download_never_becomes_installable(tmp_path, received):
    release = client.parse_release(release_data(), None)
    with pytest.raises(client.UpdateError):
        client.download(release, tmp_path, client_factory=http_factory(lambda _: httpx.Response(200, content=received)))
    assert not list(tmp_path.iterdir())


def test_cancelled_download_cleans_partial_file(tmp_path):
    cancelled = threading.Event()
    payload = b'x' * 700000
    release = client.parse_release(release_data(payload), None)
    with pytest.raises(client.UpdateError, match='取消'):
        client.download(release, tmp_path, cancelled=cancelled, progress=lambda *_: cancelled.set(),
                        client_factory=http_factory(lambda _: httpx.Response(200, content=payload)))
    assert not list(tmp_path.iterdir())


def test_zip_config_is_never_extracted(tmp_path):
    archive = make_bundle(tmp_path, {'声笺/data/config.json': 'do not overwrite', '声笺/models/local.gguf': 'private'})
    staging = tmp_path / 'staging'
    documents = manifest.extract_verified(archive, staging, client.configuration(), '0.8.0')
    assert manifest.UPDATER in documents and manifest.MANIFEST in documents
    assert not (staging / 'data').exists() and not (staging / 'models').exists()


@pytest.mark.parametrize('path', ['声笺/../outside.txt', '/outside.txt', '声笺/_internal/a.dll:stream',
                                '声笺/_internal/NUL.dll', '声笺/_internal/a. ', 'outside.txt',
                                '声笺/unknown.txt', '声笺/_internal/NEW.DLL'])
def test_unsafe_zip_paths_are_rejected(tmp_path, path):
    archive = make_bundle(tmp_path, {path: 'bad'})
    with pytest.raises(ApplyError):
        manifest.validate_archive(archive, client.configuration(), '0.8.0')


def test_zip_symlink_rejected(tmp_path):
    archive = make_bundle(tmp_path)
    link = zipfile.ZipInfo('声笺/_internal/link.dll')
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, 'a') as zf:
        zf.writestr(link, 'outside.dll')
    with pytest.raises(ApplyError, match='符号链接'):
        manifest.validate_archive(archive, client.configuration(), '0.8.0')


@pytest.mark.parametrize('change', ['tampered', 'missing_helper', 'wrong_version', 'missing_manifest'])
def test_release_manifest_rejects_incomplete_or_mismatched_bundle(tmp_path, change):
    archive = make_bundle(tmp_path)
    if change == 'tampered':
        rewrite_zip(archive, {'声笺/_internal/new.dll': b'altered'})
    if change == 'missing_helper':
        rewrite_zip(archive, omit=['声笺/' + manifest.UPDATER])
    if change == 'missing_manifest':
        rewrite_zip(archive, omit=['声笺/' + manifest.MANIFEST])
    with pytest.raises(ApplyError):
        manifest.validate_archive(archive, client.configuration(), '0.8.1' if change == 'wrong_version' else '0.8.0')


def test_apply_overwrites_program_removes_stale_runtime_and_keeps_user_data(tmp_path):
    args, cfg, job = prepared_job(tmp_path)
    apply.run_update(args, cfg, Mock())
    install = Path(args.dir)
    assert (install / '声笺.exe').read_bytes().startswith(b'MZ-new')
    assert (install / 'data' / 'config.json').read_bytes() == b'personal config'
    assert not (install / '_internal' / 'stale.dll').exists()
    assert (install / manifest.UPDATER).is_file()
    assert json.loads((tmp_path / 'result.json').read_text())['status'] == 'done'
    assert not Path(args.zip).exists()
    assert not any('backup' in p.name for p in tmp_path.iterdir())


def test_update_to_compact_layout_retires_only_known_old_docs_and_keeps_complete_notices(tmp_path):
    args, cfg, job = prepared_job(tmp_path)
    candidate = tmp_path / 'candidate'
    (candidate / 'README.md').unlink()
    license_file = candidate / '_internal/legal/licenses/pyqt6/LICENSE'
    license_file.parent.mkdir(parents=True)
    license_file.write_bytes(b'complete original license')
    guide = candidate / '_internal/docs/QWEN_BATCH_SETUP.md'
    guide.parent.mkdir(parents=True)
    guide.write_text('installation help')
    manifest.write_manifest(candidate, job['version'])
    with zipfile.ZipFile(args.zip, 'w') as zf:
        for path in candidate.rglob('*'):
            if path.is_file():
                zf.write(path, '声笺/' + path.relative_to(candidate).as_posix())
    job['sha256'] = manifest.digest_file(args.zip)
    (tmp_path / 'job.json').write_text(json.dumps(job), encoding='utf-8')
    install = Path(args.dir)
    for name in ('docs/ARCHITECTURE.md', 'docs/images/workbench.png', 'licenses/pyqt6/LICENSE', 'README.md'):
        path = install / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'old public document')
    (install / 'licenses/personal.txt').write_bytes(b'personal file')
    apply.run_update(args, cfg, Mock())
    assert not (install / 'docs').exists() and not (install / 'README.md').exists()
    assert not (install / 'licenses/pyqt6').exists()
    assert (install / 'licenses/personal.txt').read_bytes() == b'personal file'
    assert (install / 'data/config.json').read_bytes() == b'personal config'
    assert (install / '_internal/legal/licenses/pyqt6/LICENSE').read_bytes() == b'complete original license'
    assert (install / '_internal/docs/QWEN_BATCH_SETUP.md').is_file()


def test_wait_timeout_does_not_replace_any_files(tmp_path, monkeypatch):
    args, cfg, _ = prepared_job(tmp_path)
    monkeypatch.setattr(apply, 'wait_for_process_exit', lambda *_: False)
    with pytest.raises(ApplyError, match='仍在运行'):
        apply.run_update(args, cfg, Mock())
    assert (Path(args.dir) / '声笺.exe').read_bytes() == b'MZ-old'


def test_damaged_download_does_not_signal_host_to_exit(tmp_path):
    args, cfg, _ = prepared_job(tmp_path)
    Path(args.zip).write_bytes(b'corrupt')
    with pytest.raises(ApplyError, match='SHA-256'):
        apply.run_update(args, cfg, Mock())
    assert not (tmp_path / 'ready').exists()


def test_payload_changed_while_waiting_cannot_bypass_release_checksum(tmp_path, monkeypatch):
    args, cfg, _ = prepared_job(tmp_path)
    def changed(*_):
        with zipfile.ZipFile(args.zip) as zf:
            description = json.loads(zf.read('声笺/' + manifest.MANIFEST))
        description['files']['README.md'] = hashlib.sha256(b'changed').hexdigest()
        rewrite_zip(Path(args.zip), {'声笺/README.md': b'changed',
                                     '声笺/' + manifest.MANIFEST: json.dumps(description).encode()})
        return True
    monkeypatch.setattr(apply, 'wait_for_process_exit', changed)
    with pytest.raises(ApplyError, match='发生变化'):
        apply.run_update(args, cfg, Mock())
    assert (Path(args.dir) / '声笺.exe').read_bytes() == b'MZ-old'


@pytest.mark.parametrize('directory', ['_internal/data', 'docs/data', 'licenses/data'])
def test_data_cannot_be_inside_managed_program_folders(tmp_path, directory):
    with pytest.raises(ApplyError, match='迁移'):
        apply.check_data_boundary(tmp_path, tmp_path / directory)


def test_restart_uses_same_port_data_and_fresh_pyinstaller_environment(tmp_path, monkeypatch):
    args, cfg, job = prepared_job(tmp_path)
    args.no_restart = False
    popen = Mock(return_value=Mock(poll=lambda: None, pid=321))
    monkeypatch.setattr(apply.subprocess, 'Popen', popen)
    monkeypatch.setattr(apply.time, 'sleep', lambda _: None)
    apply.run_update(args, cfg, Mock())
    assert popen.call_args.args[0] == [str(Path(args.dir) / '声笺.exe'), 'gui', '--port', '8877', '--data-dir', job['data_dir']]
    assert popen.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'] == '1'
    assert json.loads((tmp_path / 'result.json').read_text())['restart_pid'] == 321


def test_vendor_configuration_roundtrip_keeps_wait_policy(tmp_path):
    cfg = client.configuration()
    path = cfg.save(tmp_path / 'config.json')
    restored = type(cfg).load(path)
    assert restored.exit_wait_seconds == 120
    assert restored.health_check_seconds == cfg.health_check_seconds


@pytest.fixture
def update_controls(qtbot, monkeypatch):
    from PyQt6.QtWidgets import QWidget
    from voice_workbench.desktop.settings.common import SettingsComponents
    from voice_workbench.desktop.settings import app_updates as gui
    monkeypatch.setattr(gui, 'frozen', lambda: False)
    class Settings(SettingsComponents, QWidget):
        pass
    settings = Settings()
    settings.owner = SimpleNamespace(workers=set(), closing=False, submitting=False, ready=False,
                                     owns_service=False, close=Mock(), setEnabled=Mock(), port=8877)
    control = gui.AppUpdateControls(settings)
    qtbot.addWidget(settings)
    yield control, gui
    control.close_updates()
    for worker in list(settings.owner.workers):
        worker.wait(15000)


def test_source_check_displays_new_version_without_enabling_install(update_controls, qtbot, monkeypatch):
    control, gui = update_controls
    release = client.parse_release(release_data(version='99.0.0'), None)
    monkeypatch.setattr(gui, 'check_release', lambda **_: release)
    control.check()
    qtbot.waitUntil(lambda: control.worker is None)
    assert '99.0.0' in control.status.text()
    assert not control.install_button.isEnabled()
    assert control.notes.toPlainText() == release.notes


def test_gui_check_failure_is_not_latest_message(update_controls, qtbot, monkeypatch):
    control, gui = update_controls
    def fail(**_):
        raise client.UpdateError('无法检查更新')
    monkeypatch.setattr(gui, 'check_release', fail)
    control.check()
    qtbot.waitUntil(lambda: control.worker is None)
    assert control.status.text() == '无法检查更新'
    assert control.check_button.isEnabled()


@pytest.mark.parametrize('busy', ['submitting', 'external_service', 'queued'])
def test_busy_gui_does_not_launch_updater(update_controls, monkeypatch, tmp_path, busy):
    control, gui = update_controls
    monkeypatch.setattr(gui, 'frozen', lambda: True)
    launcher = Mock()
    monkeypatch.setattr(gui, 'launch_updater', launcher)
    control.archive = tmp_path / 'update.zip'
    control.release = client.parse_release(release_data(), None)
    owner = control.owner
    owner.submitting = busy == 'submitting'
    owner.ready = busy != 'submitting'
    owner.owns_service = busy == 'queued'
    owner.call = lambda action, success, failed: success({'queue': 1})
    control.install()
    assert not launcher.called and not owner.close.called
    assert '请先' in control.status.text() or '其他进程' in control.status.text()


def test_helper_prepares_before_gui_closes_and_discards_stale_handshake(update_controls, monkeypatch, tmp_path):
    control, gui = update_controls
    archive = make_bundle(tmp_path)
    install = tmp_path / 'installed'
    install.mkdir()
    (install / manifest.UPDATER).write_bytes(b'MZ-helper')
    control.archive = archive
    control.release = client.parse_release(release_data(archive.read_bytes()), None)
    control.owner.data_dir = tmp_path / 'profile'
    control.owner.process = None
    (tmp_path / 'ready').touch()
    monkeypatch.setattr(gui, 'frozen', lambda: True)
    monkeypatch.setattr(gui, 'project_root', lambda: install)
    launcher = Mock(return_value=Mock(poll=lambda: None))
    monkeypatch.setattr(gui, 'launch_updater', launcher)
    control.install()
    assert launcher.called and not (tmp_path / 'ready').exists() and not control.owner.close.called
    (tmp_path / 'ready').touch()
    control.helper_ready()
    assert control.owner.close.called
