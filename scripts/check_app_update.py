"""Run the frozen updater in an isolated installation and verify its restart."""
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench import __version__
from voice_workbench.app_updates.client import configuration
from voice_workbench.app_updates.manifest import digest_file, UPDATER


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, default=ROOT / 'dist' / f'shengjian-v{__version__}-windows-x64.zip')
    parser.add_argument('--updater', type=Path, default=ROOT / 'dist' / '声笺' / UPDATER)
    args = parser.parse_args()
    test = ROOT / 'artifacts' / 'app-update-check' / datetime.now().strftime('%Y%m%d-%H%M%S')
    test.mkdir(parents=True, exist_ok=False)
    install, work = test / 'installed', test / 'cache'
    install.mkdir()
    work.mkdir()
    old = install / '声笺.exe'
    old.write_bytes(b'MZ-isolated-old-program')
    (install / '_internal').mkdir()
    (install / '_internal' / 'stale-update-check.dll').write_bytes(b'stale')
    keep = install / 'data' / 'personal.txt'
    keep.parent.mkdir()
    keep.write_bytes(b'keep config and audio')
    (install / 'Config').mkdir()
    (install / 'Config' / 'keep.txt').write_bytes(b'keep portable config')
    archive = work / args.archive.name
    shutil.copy2(args.archive, archive)
    runner = work / UPDATER
    shutil.copy2(args.updater, runner)
    cfg = configuration()
    cfg.exit_wait_seconds = 60
    cfg.save(work / 'updater-config.json')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    job = {'install': str(install), 'archive': str(archive), 'sha256': digest_file(archive),
           'version': __version__, 'port': port, 'data_dir': str(keep.parent), 'service_pid': 0}
    (work / 'job.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONUTF8='1', PYINSTALLER_RESET_ENVIRONMENT='1',
               PATH=os.pathsep.join(str(p) for p in (windows, windows / 'System32')))
    blocker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'], creationflags=subprocess.CREATE_NO_WINDOW)
    helper = None
    restarted = None
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False)
    try:
        helper = subprocess.Popen([str(runner), '--pid', str(blocker.pid), '--zip', str(archive), '--dir', str(install),
                                   '--config', str(work / 'updater-config.json'), '--no-ui'], cwd=work, env=env,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 45
        while not (work / 'ready').exists():
            if helper.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('冻结更新器未完成校验交接，请查看 ' + str(work / 'updater.log'))
            time.sleep(.1)
        assert old.read_bytes() == b'MZ-isolated-old-program', '未等原进程退出就覆盖了程序'
        assert blocker.poll() is None
        blocker.terminate()
        blocker.wait(timeout=5)
        assert helper.wait(timeout=90) == 0, '更新器失败，请查看 ' + str(work / 'updater.log')
        result = json.loads((work / 'result.json').read_text())
        restarted = result.get('restart_pid')
        assert restarted and result['status'] == 'done'
        deadline = time.monotonic() + 30
        while True:
            try:
                health = client.get('/health').json()
                if health['version'] == __version__ and Path(health['data_dir']).resolve() == keep.parent.resolve():
                    break
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('更新后没有使用原端口与数据目录启动服务')
            time.sleep(.1)
        assert keep.read_bytes() == b'keep config and audio'
        assert (install / 'Config' / 'keep.txt').read_bytes() == b'keep portable config'
        assert not (install / '_internal' / 'stale-update-check.dll').exists()
        assert not (install / '.backup-update').exists() and not archive.exists()
        report = dict(result, version=__version__, data_preserved=True, port_preserved=True,
                      process_wait_verified=True, no_python_on_path=True, no_old_program_backup=True,
                      restarted_service=True, isolated_install=str(install))
        (test / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('冻结更新器覆盖 / 等待 / 重启验证通过：' + str(test / 'report.json'))
    finally:
        try:
            health = client.get('/health').json()
            if Path(health.get('data_dir', '.')).resolve() == keep.parent.resolve():
                client.post('/shutdown')
        except (httpx.HTTPError, ValueError):
            pass
        client.close()
        if restarted:
            try:
                os.kill(restarted, signal.SIGTERM)
            except OSError:
                pass
        for process in (helper, blocker):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
