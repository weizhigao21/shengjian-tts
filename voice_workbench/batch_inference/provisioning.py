"""Cancellable managed runtime/model installation, separate from inference."""
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time

import httpx

from ..config import atomic_json
from ..downloads import DownloadPaused, TransferRate, download_file, transfer_state
from ..errors import VoiceError
from ..engine_sources import SOURCE_NAMES, download_archive
from ..model_updates import safe_remote_path
from ..program_files import checked_child
from ..runtime import external_environment
from .discovery import catalog, discover, model_path, python_path
from .sources import MODEL_SOURCES, RUNTIME_SOURCES, model_url, pip_plans

BUSY = {'detecting', 'downloading', 'verifying', 'extracting', 'dependencies', 'checking', 'waiting', 'pausing'}


class BatchSetup:
    def __init__(self, backend, client_factory=None):
        self.backend = backend
        self.root = backend.manager.root
        self.lock = threading.RLock()
        self.thread = None
        self.event = threading.Event()
        self.closed = False
        self.state = {'status': 'idle', 'received': 0, 'size': 0, 'speed_bps': 0, 'message': ''}
        self.client_factory = client_factory or (lambda: httpx.Client(follow_redirects=True, trust_env=False,
            timeout=httpx.Timeout(20, connect=10, read=10)))
        self.last_scan = 0
        self.last_scan_key = None
        self.runtime_source = 'mirror'

    @property
    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def update(self, **changes):
        with self.lock:
            self.state.update(changes)

    def status(self):
        with self.lock:
            return {**transfer_state(self.state), 'busy': self.busy}

    def options(self):
        candidate, available, selected = discover(self.backend)
        data = catalog()
        return {'sources': available, 'selected': selected, 'detected': candidate.model_dump(),
                'directory': str(self.root), 'python_download_size': data['python']['size'],
                'model_download_size': sum(f['size'] for f in data['models'][selected['size']]['files']) if selected else 0,
                'task': self.status(), 'supported': os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64')}

    def ensure_scan(self):
        # Polls never start downloads, import torch, or run dependency checks repeatedly.
        now = time.monotonic()
        if now - self.last_scan < 10 or self.busy or self.closed or self.state['status'] in ('paused', 'failed'):
            return
        if getattr(self.backend.manager, 'benchmark', None) and self.backend.manager.benchmark.gpu_busy:
            return
        self.last_scan = now
        candidate, _, _ = discover(self.backend)
        interpreter = Path(candidate.python) if candidate.python else None
        probes = [Path(candidate.model_dir) / 'config.json', Path(candidate.model_dir) / 'shengjian-source.json'] if candidate.model_dir else []
        if interpreter:
            probes += [interpreter, interpreter.parent / 'Lib/site-packages/qwen_tts/__init__.py',
                       interpreter.parent.parent / 'Lib/site-packages/qwen_tts/__init__.py']
        stamps = []
        for path in probes:
            try:
                stat = path.stat()
                stamps.append((str(path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                stamps.append((str(path), None))
        key = (candidate.model_dump_json(), stamps)
        if key == self.last_scan_key:
            return
        self.last_scan_key = key
        if candidate != self.backend.settings or all((candidate.python, candidate.model_dir, candidate.source_model)):
            self.start('detect')

    def start(self, action='all', source_model='', device=None, source='modelscope', runtime_source='mirror'):
        if (action not in ('all', 'runtime', 'model', 'detect') or source not in ('mirror', 'huggingface', 'modelscope')
                or runtime_source not in RUNTIME_SOURCES):
            raise VoiceError('BATCH_SETUP_INVALID', '安装操作或下载源无效', stage='批量安装')
        with self.backend.manager.lock, self.lock:
            if self.closed or self.backend.manager.stopping.is_set():
                raise VoiceError('SERVICE_STOPPING', '服务正在退出', stage='批量安装')
            if self.busy:
                raise VoiceError('DOWNLOAD_BUSY', '请先完成或暂停当前批量安装', stage='批量安装')
            candidate, _, selected = discover(self.backend, source_model, device)
            if source_model and selected is None:
                raise VoiceError('BATCH_MODEL_UNSUPPORTED', '请选择已导入的 Qwen3-TTS 0.6B / 1.7B Base GGUF 模型', stage='批量安装')
            if action in ('all', 'model') and selected is None:
                raise VoiceError('BATCH_MODEL_UNSUPPORTED', '请先在模型页面下载或导入 Qwen3-TTS Base GGUF', stage='批量安装')
            if action != 'detect' and (os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64')):
                raise VoiceError('BATCH_PLATFORM_UNSUPPORTED', '自动安装目前支持 Windows x64；其他平台可使用高级手动配置。', stage='批量安装')
            if action != 'detect':
                self.backend.manager.require_idle_engine('批量安装')
                self.backend.process.interrupt()
            self.event = threading.Event()
            self.runtime_source = runtime_source
            self.state = {'status': 'detecting', 'action': action, 'received': 0, 'size': 0, 'speed_bps': 0,
                          'message': '正在检测本地环境和关联模型…', 'source_model': candidate.source_model,
                          'source': source, 'runtime_source': runtime_source}
            original = self.backend.settings.model_dump()
            self.thread = threading.Thread(target=self._run, args=(action, candidate, selected, source, original),
                                           name='qwen-batch-setup', daemon=True)
            self.thread.start()
        return self.status()

    def pause(self):
        with self.lock:
            if self.busy:
                self.event.set()
                self.state.update(status='pausing', speed_bps=0, message='正在暂停；已下载文件会保留。')
        return self.status()

    def check_cancel(self):
        if self.event.is_set():
            raise DownloadPaused()

    def managed(self, path):
        return checked_child(Path(path), self.root)

    def _extract(self, archive, target):
        self.managed(target)
        with tarfile.open(archive, 'r:gz') as package:
            members = package.getmembers()
            if len(members) > 30000 or sum(m.size for m in members) > 1024**3:
                raise ValueError('Python 压缩包大小异常')
            for member in members:
                self.check_cancel()
                if not safe_remote_path(member.name) or not (member.isfile() or member.isdir()):
                    raise ValueError('Python 压缩包包含无效路径或链接')
                output = target / member.name
                if not output.resolve().is_relative_to(target.resolve()):
                    raise ValueError('Python 解压路径超出安装目录')
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with package.extractfile(member) as stream, output.open('wb') as dest:
                        shutil.copyfileobj(stream, dest)

    def _command(self, command, timeout=3600):
        self.check_cancel()
        logs = self.root / 'logs/qwen-batch-install.log'
        temporary = self.managed(self.root / 'temporary/qwen-install')
        temporary.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PIP_DISABLE_PIP_VERSION_CHECK='1',
                   PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT='1', PIP_CACHE_DIR=str(self.root / 'runtime/qwen-batch/pip-cache'),
                   TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
        for key in list(env):
            if key.startswith(('PIP_', 'PYTHON')) and key not in ('PYTHONUTF8', 'PYTHONIOENCODING', 'PIP_DISABLE_PIP_VERSION_CHECK', 'PIP_CONFIG_FILE', 'PIP_NO_INPUT', 'PIP_CACHE_DIR'):
                env.pop(key)
        with external_environment(env) as clean:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', env=clean, cwd=self.root,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')
        lines = queue.Queue(maxsize=2000)
        def read():
            for line in proc.stdout:
                try: lines.put_nowait(line[:8192])
                except queue.Full: pass
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        last, rate, tail = 0, TransferRate(0), []
        try:
            with logs.open('a', encoding='utf-8') as log:
                while proc.poll() is None or reader.is_alive() or not lines.empty():
                    self.check_cancel()
                    if time.monotonic() > deadline:
                        raise TimeoutError('依赖安装超时，请重试或更换网络。')
                    try: line = lines.get(timeout=.1).strip()
                    except queue.Empty: continue
                    if not line: continue
                    log.write(line + '\n'); log.flush()
                    tail = (tail + [line])[-8:]
                    match = re.fullmatch(r'Progress (\d+) of (\d+)', line)
                    if match:
                        received, total = map(int, match.groups())
                        if received < last: rate = TransferRate(0)
                        last = received
                        self.update(status='downloading', received=received, size=total,
                                    speed_bps=rate.sample(received), speed_sampled_at=time.monotonic())
                    else:
                        self.update(message=line[:240])
                if proc.wait() != 0:
                    raise RuntimeError('依赖安装或检查失败：' + '\n'.join(tail))
        finally:
            if proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    if proc.poll() is None:
                        proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
            proc.stdout.close()
            reader.join(timeout=2)

    def _pip(self, prefix, packages, device=None):
        errors = []
        for name, command in pip_plans(prefix, packages, self.runtime_source, device):
            self.check_cancel()
            self.update(status='dependencies', received=0, size=0, speed_bps=0,
                        download_source=name, source_errors=list(errors), message='正在安装依赖 · ' + name)
            try:
                self._command(command)
                return
            except (RuntimeError, TimeoutError) as exc:
                self.check_cancel()
                errors.append({'source': name, 'message': str(exc)})
                self.update(source_errors=list(errors), message=name + ' 未完成，正在尝试其他来源…')
        detail = '\n'.join(e['source'] + '：' + e['message'] for e in errors)
        raise RuntimeError('依赖安装失败；可暂停后更换运行环境来源重试。\n' + detail)

    def _runtime(self, device):
        root = self.managed(self.root / 'runtime/qwen-batch')
        root.mkdir(parents=True, exist_ok=True)
        base = root / 'python/python.exe'
        if not base.is_file():
            entry = catalog()['python']
            self.update(size=entry['size'], phase='python', message='下载独立 Python 运行环境…')
            archive = root / 'downloads' / entry['name']
            self.managed(archive)
            archive.parent.mkdir(parents=True, exist_ok=True)
            def progress(**changes):
                if 'active_source' in changes:
                    changes['download_source'] = SOURCE_NAMES[changes['active_source']]
                self.update(**changes)
            download_archive(archive, entry, entry['url'], self.event, self.client_factory, progress,
                             'auto' if self.runtime_source == 'mirror' else 'github', label='Python')
            self.update(status='extracting', speed_bps=0, message='解压独立 Python…')
            with tempfile.TemporaryDirectory(prefix='.python-', dir=root) as folder:
                staging = Path(folder)
                self._extract(archive, staging)
                if not (staging / 'python/python.exe').is_file():
                    raise ValueError('运行环境未包含 Python')
                self.check_cancel()
                if base.parent.exists():
                    raise ValueError('Python 安装目录不完整，请在数据目录移除 runtime/qwen-batch/python 后重试。')
                (staging / 'python').replace(base.parent)
        env = self.managed(root / ('env-' + device))
        python = python_path(env)
        marker = env / 'shengjian-runtime.json'
        identity = {'torch': '2.9.0', 'qwen-tts': '0.1.1', 'device': device}
        try:
            installed = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            installed = None
        if installed != identity or self.backend.diagnose_python(str(python))[0]:
            self.update(status='dependencies', phase='environment', received=0, size=0, speed_bps=0, message='创建或修复专用环境…')
            self._command([str(base), '-m', 'venv', str(env)], timeout=120)
            self.update(status='dependencies', phase='dependencies', received=0, size=0, speed_bps=0, message='下载并安装 PyTorch 和 Qwen SDK；首次需要数 GB…')
            prefix = [str(python), '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check']
            self._pip(prefix, ['pip==25.2'])
            self._pip(prefix + ['--progress-bar', 'raw'], [], device=device)
            self._pip(prefix + ['--progress-bar', 'raw'], ['qwen-tts==0.1.1'])
        self.update(status='checking', phase='runtime', speed_bps=0, message='检查独立环境和计算设备…')
        code = 'import torch,qwen_tts,soundfile; print("Qwen runtime ready"); '
        if device == 'cuda':
            code += 'assert torch.cuda.is_available(), "CUDA 不可用，请更新显卡驱动或选择 CPU 环境"; torch.empty(1,device="cuda")'
        self._command([str(python), '-c', code], timeout=120)
        atomic_json(marker, identity)
        return str(python)

    def _model(self, size, source):
        entry = catalog()['models'][size]
        root = self.managed(model_path(self.root, size))
        total = sum(f['size'] for f in entry['files'])
        completed = 0
        self.update(size=total, received=0, phase='model', download_source=MODEL_SOURCES[source],
                    source_errors=[], message='下载对应的 Qwen Base 原始权重…')
        for file in entry['files']:
            self.check_cancel()
            if not safe_remote_path(file['path']):
                raise ValueError('模型目录包含无效路径')
            target = self.managed(root / file['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            def progress(**changes):
                if 'received' in changes: changes['received'] += completed
                self.update(message=file['path'], **changes)
            try:
                download_file(target, file, model_url(entry, file, source),
                              self.event, self.client_factory, progress)
            except ValueError:
                target.unlink(missing_ok=True)
                raise
            completed += file['size']
        atomic_json(root / 'shengjian-source.json', {'repo': entry['repo'], 'revision': entry['revision']})
        return str(root)

    def _apply(self, candidate, original):
        while True:
            self.check_cancel()
            with self.backend.manager.lock:
                if self.backend.settings.model_dump() != original:
                    raise ValueError('安装期间配置已更改，文件已保留；请点击自动检测重新关联。')
                idle = not self.backend.manager.active and not self.backend.manager.editing_busy
            if idle:
                try:
                    self.backend.configure(candidate, from_setup=True)
                    return
                except VoiceError as exc:
                    if exc.code != 'ENGINE_BUSY':
                        raise
            self.update(status='waiting', speed_bps=0, message='文件已就绪，等待当前任务结束后自动关联…')
            self.event.wait(.5)

    def _run(self, action, candidate, selected, source, original):
        try:
            if action in ('all', 'runtime') and not (candidate.python and not self.backend.diagnose_python(candidate.python)[0]):
                candidate = candidate.model_copy(update={'python': self._runtime(candidate.device)})
            if action in ('all', 'model') and not candidate.model_dir:
                candidate = candidate.model_copy(update={'model_dir': self._model(selected['size'], source)})
            self.check_cancel()
            self.update(status='checking', speed_bps=0, message='检查并自动关联本地环境和模型…')
            self._apply(candidate, original)
            self.update(status='done', speed_bps=0, message='自动配置完成，批量后端已就绪。' if not self.backend.reason
                        else '已保存检测到的配置：' + self.backend.reason)
        except DownloadPaused:
            self.update(status='paused', speed_bps=0, message='已暂停，已下载文件保留；点击继续安装即可复用。')
        except Exception as exc:
            self.update(status='failed', speed_bps=0, message=str(exc))

    def close(self):
        self.closed = True
        self.event.set()
        if self.thread:
            self.thread.join(timeout=15)
