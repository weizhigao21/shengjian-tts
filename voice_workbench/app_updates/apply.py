"""Independent updater: wait for host processes, replace known program files, restart."""
import argparse
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from .._vendor.pyupdater.apply import wait_for_process_exit, ApplyError
from .._vendor.pyupdater.config import UpdateConfig
from .._vendor.pyupdater.progress import ProgressWindow
from ..program_files import overwrite_program
from .manifest import digest_file, validate_archive, extract_verified, check_data_boundary


def load_job(args, cfg):
    config = Path(args.config).resolve()
    job = json.loads((config.parent / 'job.json').read_text(encoding='utf-8'))
    install, archive = Path(args.dir).resolve(), Path(args.zip).resolve()
    if install != Path(job['install']).resolve() or archive != Path(job['archive']).resolve():
        raise ApplyError("更新参数与准备的任务不一致")
    if archive.parent != config.parent or config.parent.is_relative_to(install):
        raise ApplyError("更新器必须在安装目录以外运行")
    if not (install / cfg.exe_name).is_file():
        raise ApplyError("更新目标没有声笺主程序")
    check_data_boundary(install, job['data_dir'])
    if digest_file(archive) != job['sha256']:
        raise ApplyError("更新包 SHA-256 不匹配")
    validate_archive(archive, cfg, job['version'])
    return job


def run_update(args, cfg, ui):
    job = load_job(args, cfg)
    work = Path(args.config).resolve().parent
    # Host closes only after this validated handshake is visible.
    (work / 'ready').write_text('ready', encoding='ascii')
    ui.status('等待声笺和本地服务退出…')
    deadline = time.monotonic() + cfg.exit_wait_seconds
    for pid in (args.pid, job.get('service_pid', 0)):
        if not wait_for_process_exit(int(pid), max(0, deadline - time.monotonic())):
            raise ApplyError('程序或服务仍在运行，未覆盖任何程序文件')
    install = Path(args.dir).resolve()
    archive = Path(args.zip).resolve()
    if digest_file(archive) != job['sha256']:
        raise ApplyError('等待进程退出期间更新包发生变化，未覆盖任何程序文件')
    with tempfile.TemporaryDirectory(prefix='payload-', dir=work) as folder:
        staging = Path(folder).resolve()
        if staging.parent != work:
            raise ApplyError('更新暂存目录异常')
        ui.status('校验并展开程序文件…')
        documentation = extract_verified(archive, staging, cfg, job['version'])
        ui.status('覆盖程序文件，保留配置和数据…')
        count = overwrite_program(staging, install, documentation)
    restart_pid = None
    if not args.no_restart:
        ui.status('启动新版本…')
        process = subprocess.Popen([str(install / cfg.exe_name), 'gui', '--port', str(job['port']),
                                    '--data-dir', job['data_dir']], cwd=install, close_fds=True,
                                   env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
        time.sleep(cfg.health_check_seconds)
        if process.poll() is not None:
            raise ApplyError('新版启动失败，请从发行页重新下载完整包进行修复；配置和数据仍保留')
        restart_pid = process.pid
    (work / 'result.json').write_text(json.dumps({'status': 'done', 'files': count, 'restart_pid': restart_pid}), encoding='utf-8')
    archive.unlink(missing_ok=True)
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description='声笺独立更新器')
    parser.add_argument('--pid', type=int, default=0)
    parser.add_argument('--zip', required=True)
    parser.add_argument('--dir', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--no-ui', action='store_true')
    parser.add_argument('--no-restart', action='store_true', help='验证模式：安装后不启动界面')
    args = parser.parse_args(argv)
    work = Path(args.config).resolve().parent
    logging.basicConfig(filename=str(work / 'updater.log'), encoding='utf-8', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    ui = ProgressWindow('声笺', enabled=not args.no_ui)
    try:
        cfg = UpdateConfig.load(args.config)
        run_update(args, cfg, ui)
    except Exception as exc:
        logging.exception('更新失败')
        (work / 'result.json').write_text(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), encoding='utf-8')
        ui.error('更新未完成：' + str(exc))
        return 1
    finally:
        ui.close()
    return 0
