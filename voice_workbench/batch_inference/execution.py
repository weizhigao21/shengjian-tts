"""Queue integration, isolated result cache, cancellation and one-time native fallback."""
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

from ..config import atomic_json, file_digest, voice_fingerprint
from ..errors import describe_error, VoiceError
from ..runner import validate_wave
from .policy import LANGUAGES, PARAMETERS, model_signature, options


class BatchExecution:
    def _execute_batch(self, slot, job, items, cancelled):
        paths = [self.root / 'temporary' / f"{job['id']}-{item['idx']}-batch.wav" for item in items]
        try:
            for item in items:
                if voice_fingerprint(item["voice"]) != item["fingerprint"]:
                    raise VoiceError("VOICE_VERSION_CHANGED", "排队期间模型或参考音频发生变化")
            settings = self.batch.settings
            signature = model_signature(settings, self.batch.versions)
            # Batch composition affects RNG consumption. Partition the result cache
            # by the complete ordered batch as well as model/backend identity.
            composition = [(i["fingerprint"], i["text"]) for i in items]
            cohort = hashlib.sha256((signature + json.dumps(composition, ensure_ascii=False)).encode()).hexdigest()
            targets, keys, cached = [], [], []
            for position, item in enumerate(items):
                key = hashlib.sha256(f"qwen-batch-v1:{cohort}:{position}".encode()).hexdigest()
                transient = item["audio_storage"] == "temporary"
                if transient or item.get("force_generate"):
                    key = hashlib.sha256(f"take:{job['id']}:{item['idx']}:{key}".encode()).hexdigest()
                output = (self.workflow_audio.root if transient else self.root / 'audio') / (key + '.wav')
                record = None if transient or item.get("force_generate") else self.store.cache_get(key)
                valid = False
                if record:
                    try:
                        validate_wave(output)
                        valid = record['checksum'] == file_digest(str(output))
                    except Exception:
                        pass
                targets.append(output); keys.append(key); cached.append(valid)
            elapsed, answer = 0.0, {}
            if not all(cached):
                with self.lock:
                    if cancelled.is_set() or self.stopping.is_set():
                        return
                    # Retain only one resident inference backend on the GPU.
                    for runner in self.runners.values():
                        runner.interrupt()
                first = items[0]['voice']
                request = options(first)
                payload = {"voice": {k: first[k] for k in ('reference_audio', 'reference_text')},
                           "seed": request.get('seed', -1),
                           "cpu_threads": self.config.performance()['cpu_threads'],
                           "options": {target: request[source] for source, target in PARAMETERS.items() if source in request},
                           "items": [{"text": item['text'], "language": LANGUAGES[item['voice']['language']], "output": str(path)}
                                     for item, path in zip(items, paths)]}
                started = perf_counter()
                self.execution_phase(slot, 'generating')
                answer = self.batch.process.generate(settings, signature, payload, cancelled,
                    max(i['voice']['timeout'] for i in items))
                elapsed = perf_counter() - started
                if answer.get('count') != len(items):
                    raise VoiceError('BATCH_RESULT_COUNT', '批量音频数量与请求不符')
                # Validate every output before committing any result. A malformed
                # batch cannot leave half a batch marked successful.
                for path in paths:
                    validate_wave(path)
                if model_signature(settings, self.batch.versions) != signature:
                    raise VoiceError('BATCH_MODEL_CHANGED', '批量推理期间原始模型文件发生变化')
            metadata = {"backend": 'qwen3_batch', "batch_size": len(items), "batch_seconds": elapsed,
                        "model_signature": signature, **{k: answer[k] for k in
                        ('warm_model', 'prompt_cache_hit', 'load_seconds', 'prompt_seconds', 'inference_seconds',
                         'predictor_mode', 'predictor_revision', 'predictor_calls', 'predictor_fallback_calls', 'predictor_reason') if k in answer}}
            with self.lock:
                if cancelled.is_set() or self.stopping.is_set():
                    return
                for item, path, output, key in zip(items, paths, targets, keys):
                    if not all(cached):
                        os.replace(path, output)
                        if item['audio_storage'] != 'temporary':
                            self.store.cache_put(key, output, file_digest(str(output)))
                    details = validate_wave(output)
                    self.store.finish(job['id'], item['idx'], audio=output.name, cached=all(cached),
                        duration=details['duration'], generation_seconds=elapsed, inference=metadata)
                self.batch.last_result = dict(metadata, job_id=job['id'], indices=[i['idx'] for i in items])
                self.execution_finished(slot, 'done', cached=all(cached), inference=metadata)
        except Exception as exc:
            self.batch.process.interrupt()
            if not cancelled.is_set() and not self.stopping.is_set():
                error = describe_error(exc, '批量推理')
                try:
                    atomic_json(self.root / 'logs' / f"batch-{job['id']}-{items[0]['idx']}.json",
                                {'error': error, 'traceback': getattr(exc, 'engine_traceback', '')})
                except OSError:
                    pass
                with self.lock:
                    if cancelled.is_set() or self.stopping.is_set():
                        return
                    self.batch.fallback(job, items[0], error['message'])
                    self.execution_finished(slot, 'fallback', inference={'fallback_reason': error['message']})
                    for item in items:
                        self.store.requeue(job['id'], item['idx'])
        finally:
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            with self.lock:
                if cancelled.is_set():
                    self.execution_finished(slot, 'cancelled')
                self.active.pop(slot, None)
                self.current = next(((a['job_id'], a['index']) for a in self.active.values()), None)
                self.wake.set()
