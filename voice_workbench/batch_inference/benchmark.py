"""Reserved, cache-free native/GPU batch comparisons with bounded workloads."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid

from ..audio_cpp import AudioCppRunner
from ..config import atomic_json
from ..errors import VoiceError, describe_error
from ..runner import check_voice, validate_wave
from .policy import LANGUAGES, PARAMETERS, model_signature, options, unsupported
from .process import BatchProcess

ZH = ['今天我们一起测试语音生成的速度。', '相同的音色可以重复使用参考特征。',
      '每个字幕片段都会生成对应的声音。', '现在开始处理下一组简短的文字。']
EN = ['Today we are testing speech generation speed.', 'The same voice can reuse its reference features.',
      'Each subtitle segment gets its own audio.', 'Now we can process the next group of short sentences.']


def workload(name):
    if name == 'zh': return [{'text': t, 'language': 'zh'} for t in ZH]
    if name == 'en': return [{'text': t, 'language': 'en'} for t in EN]
    return [{'text': text, 'language': lang} for text, lang in
            ((ZH[0], 'zh'), (EN[0], 'en'), (ZH[1], 'zh'), (EN[1], 'en'))]


def metrics(seconds, audio_seconds, count):
    return {'seconds': seconds, 'audio_seconds': audio_seconds,
            'rtf': seconds / audio_seconds, 'items_per_second': count / seconds}


class InferenceBenchmark:
    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.gpu_busy = False
        self.thread = None
        self.native = self.batch = None
        self.cleanup_errors = []
        self.native_factory = lambda root, threads: AudioCppRunner(root, cpu_threads=threads)
        self.batch_factory = BatchProcess
        self.path = manager.root / 'benchmarks/latest.json'
        try:
            self.result = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.result, dict) or not isinstance(self.result.get('results'), list):
                self.result = None
        except (OSError, ValueError):
            self.result = None
        self.state = {'status': 'idle', 'message': '', 'completed': 0, 'total': 0}

    def update(self, **changes):
        with self.lock:
            self.state.update(changes)

    def status(self):
        with self.lock:
            return {**deepcopy(self.state), 'busy': self.gpu_busy, 'result': deepcopy(self.result)}

    def options(self):
        settings = self.manager.batch.settings
        voices = [v for v in self.manager.config.voices() if not v.get('is_template')
                  and v.get('backend') == 'audio_cpp' and v.get('family') == 'qwen3_tts'
                  and settings.source_model and Path(v['model_dir']).resolve() == Path(settings.source_model).resolve()]
        reason = self.manager.batch.status()['reason']
        return {**self.status(), 'voices': [{'id': v['id'], 'name': v['name']} for v in voices],
                'ready': not bool(reason), 'reason': reason}

    def start(self, voice_id, workload_name='bilingual', warm_rounds=1):
        m = self.manager
        with m.lock, self.lock:
            m.require_idle_engine('速度测试')
            if self.gpu_busy or self.thread and self.thread.is_alive() or m.batch.setup.busy:
                raise VoiceError('ENGINE_BUSY', '请先完成当前测试或环境安装', stage='速度测试')
            if m.store.queued_job_count():
                raise VoiceError('ENGINE_BUSY', '请先完成或取消排队任务，再开始速度测试', stage='速度测试')
            if m.batch.reason:
                raise VoiceError('BATCH_NOT_READY', m.batch.reason, stage='速度测试')
            voice, _ = m.config.resolve(voice_id)
            voice = deepcopy(voice)
            # A bounded deterministic workload; both backends receive these same options.
            request = options(voice)
            request.update(seed=1234, max_tokens=min(request.get('max_tokens') or 256, 256))
            voice['request_options'] = request
            rows = workload(workload_name)
            for row in rows:
                local = dict(voice, language=row['language'])
                check_voice(local)
                if reason := unsupported(local, row['text'], m.batch.settings):
                    raise VoiceError('BENCHMARK_UNSUPPORTED', reason, stage='速度测试')
            if any(len(row['text']) > m.batch.settings.max_batch_chars for row in rows):
                raise VoiceError('BENCHMARK_UNSUPPORTED', '当前每批字数限制不足以完成速度测试', stage='速度测试')
            m.release_runners()
            self.cancelled = threading.Event()
            self.cleanup_errors = []
            self.gpu_busy = True
            self.result = None
            self.state = {'status': 'running', 'message': '准备速度测试；将依次加载两种后端…',
                          'completed': 0, 'total': 2 * len(rows) * (1 + warm_rounds), 'started': time.time()}
            settings = m.batch.settings.model_copy(deep=True)
            versions = dict(m.batch.versions)
            self.thread = threading.Thread(target=self._run, args=(voice, rows, settings, versions, warm_rounds),
                                           name='inference-benchmark', daemon=True)
            self.thread.start()
        return self.status()

    def check_cancel(self):
        if self.cancelled.is_set() or self.manager.stopping.is_set():
            raise VoiceError('CANCELLED', '速度测试已取消', stage='速度测试')

    def cancel(self):
        with self.lock:
            if not self.gpu_busy:
                return self.status()
            self.cancelled.set()
            self.state.update(status='cancelling', message='正在取消并释放测试引擎…')
            native, batch = self.native, self.batch
        # Release outside the state lock so progress polling never waits on process exit.
        threading.Thread(target=self._interrupt, args=((native, batch),), daemon=True).start()
        return self.status()

    def _interrupt(self, processes=None):
        for process in processes or (self.native, self.batch):
            if process:
                try:
                    process.interrupt()
                except Exception as exc:
                    with self.lock:
                        self.cleanup_errors.append(str(exc))

    def _round(self, backend, voice, rows, settings, signature, folder, stage, cpu_threads):
        started = time.perf_counter()
        audio_seconds, first_seconds, calls = 0., None, 0
        position = 0
        while position < len(rows):
            self.check_cancel()
            count = 1
            if backend == 'qwen3_batch':
                chars = len(rows[position]['text'])
                while position + count < len(rows) and count < settings.max_batch_size:
                    size = len(rows[position + count]['text'])
                    if chars + size > settings.max_batch_chars: break
                    chars += size; count += 1
            group = rows[position:position + count]
            paths = [folder / f'{backend}-{stage}-{position+i}.wav' for i in range(count)]
            self.update(message=f"{'原生引擎' if backend == 'native' else '批量后端'} · {stage} · {position+1}–{position+count}/{len(rows)}")
            before = time.perf_counter()
            if backend == 'native':
                self.native.generate({'voice': dict(voice, language=group[0]['language']),
                                      'text': group[0]['text'], 'output': str(paths[0])}, self.cancelled)
            else:
                request = options(voice)
                payload = {'voice': {key: voice[key] for key in ('reference_audio', 'reference_text')},
                           'seed': request['seed'], 'cpu_threads': cpu_threads,
                           'options': {target: request[source] for source, target in PARAMETERS.items() if source in request},
                           'items': [{'text': row['text'], 'language': LANGUAGES[row['language']], 'output': str(path)}
                                     for row, path in zip(group, paths)]}
                answer = self.batch.generate(settings, signature, payload, self.cancelled, voice['timeout'])
                if answer.get('count') != len(group):
                    raise VoiceError('BATCH_RESULT_COUNT', '批量后端返回的音频数量不符', stage='速度测试')
            elapsed = time.perf_counter() - before
            self.check_cancel()
            if first_seconds is None: first_seconds = elapsed
            for path in paths:
                audio_seconds += validate_wave(path)['duration']
                path.unlink()
            calls += 1
            position += count
            with self.lock:
                self.state['completed'] += count
        return {**metrics(time.perf_counter() - started, audio_seconds, len(rows)),
                'first_call_seconds': first_seconds, 'calls': calls}

    def _run(self, voice, rows, settings, versions, warm_rounds):
        started = time.perf_counter()
        report = {'id': uuid.uuid4().hex, 'started': time.time(), 'voice_id': voice['id'], 'voice_name': voice['name'],
                  'native_model': voice['model_dir'], 'batch_model': settings.model_dir, 'device': settings.device,
                  'dtype': settings.dtype, 'max_batch_size': settings.max_batch_size,
                  'seed': 1234, 'generation_options': voice['request_options'], 'workload': rows,
                  'warm_rounds': warm_rounds, 'cache_used': False, 'cache_scope': 'generated_audio',
                  'results': [], 'versions': versions}
        logs = self.manager.root / 'logs/benchmarks' / report['id']
        cpu_threads = self.manager.config.performance()['cpu_threads'] or min(8, max(1, os.cpu_count() or 4))
        report['cpu_threads'] = cpu_threads
        status, message = 'done', '速度测试完成。'
        try:
            parent = self.manager.root / 'temporary'
            with tempfile.TemporaryDirectory(prefix='benchmark-', dir=parent) as work:
                folder = Path(work)
                reference = folder / 'reference.wav'
                shutil.copy2(voice['reference_audio'], reference)
                voice = dict(voice, reference_audio=str(reference))
                signature = model_signature(settings, versions)
                for backend in ('native', 'qwen3_batch'):
                    self.check_cancel()
                    result = {'backend': backend, 'status': 'running'}
                    report['results'].append(result)
                    try:
                        if backend == 'native':
                            self.native = self.native_factory(logs/'native', cpu_threads)
                        else:
                            self.batch = self.batch_factory(logs/'batch')
                        result['cold'] = self._round(backend, voice, rows, settings, signature, folder, '首组（含加载）', cpu_threads)
                        warm = [self._round(backend, voice, rows, settings, signature, folder,
                                           f'热模型第 {i+1} 组', cpu_threads) for i in range(warm_rounds)]
                        seconds = sum(r['seconds'] for r in warm)
                        audio = sum(r['audio_seconds'] for r in warm)
                        result.update(status='done', warm_rounds=warm,
                                      warm={**metrics(seconds, audio, len(rows)*warm_rounds),
                                            'seconds': seconds/warm_rounds, 'audio_seconds': audio/warm_rounds})
                    except Exception as exc:
                        self.check_cancel()
                        result.update(status='failed', error=describe_error(exc, '速度测试'))
                    finally:
                        self._interrupt()
                        self.native = self.batch = None
                    with self.lock:
                        self.result = deepcopy(report)
                native, batch = report['results']
                if native['status'] == batch['status'] == 'done':
                    report['speedup'] = {phase: native[phase]['seconds']/batch[phase]['seconds'] for phase in ('cold', 'warm')}
                else:
                    status, message = 'failed', '部分后端测试失败，保留已完成的数据；未计算提速比例。'
        except Exception as exc:
            status = 'cancelled' if self.cancelled.is_set() or self.manager.stopping.is_set() else 'failed'
            message = '测试已取消，已释放引擎。' if status == 'cancelled' else str(exc)
            if report['results'] and report['results'][-1]['status'] == 'running':
                report['results'][-1].update(status=status, error=describe_error(exc, '速度测试'))
        finally:
            self._interrupt()
            self.native = self.batch = None
            if self.cleanup_errors:
                status, message = 'failed', '测试引擎清理失败：' + '; '.join(self.cleanup_errors)
            report.update(status=status, message=message, total_seconds=time.perf_counter()-started)
            try:
                atomic_json(self.path, report)
            except OSError as exc:
                message += ' 报告保存失败：' + str(exc)
            with self.manager.lock, self.lock:
                self.result = report
                self.state.update(status=status, message=message)
                self.gpu_busy = False
                self.manager.wake.set()

    def close(self):
        self.cancel()
        if self.thread: self.thread.join(timeout=15)
        if self.thread and self.thread.is_alive():
            raise RuntimeError('测速引擎仍在退出中，请稍后再关闭服务。')
