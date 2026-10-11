from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import secrets
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from .config import ConfigStore, atomic_json, file_digest, voice_fingerprint
from .errors import VoiceError, describe_error
from .runner import check_voice, validate_wave
from .audio_cpp import UnifiedRunner
from .store import Store
from .model_info import MODES, model_ui, selected_mode


class ServiceLock:
    """同一数据目录只允许一个推理服务，避免双重恢复和重复抢占任务。"""
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / "service.lock").open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError("此数据目录已有语音服务运行。请连接现有服务，或先停止它。")
        else:
            import fcntl
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("此数据目录已有语音服务运行")

    def close(self):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


class Manager:
    def __init__(self, data_dir: Path, runner=None, runner_factory=None):
        self.root = Path(data_dir).resolve()
        self.service_lock = ServiceLock(self.root)
        self.config = ConfigStore(self.root)
        self.store = Store(self.root)
        self.runner = runner or UnifiedRunner(self.root)
        self.runners = {0: self.runner}
        self.runner_factory = runner_factory or (lambda root: UnifiedRunner(root))
        self.parallel_supported = runner is None or runner_factory is not None
        self.active = {}
        self.serial_jobs = set()
        self.oom_retried = set()
        self.performance_notice = ""
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.current = None
        for folder in ("audio", "logs", "temporary"):
            (self.root / folder).mkdir(exist_ok=True)
        from .model_management import ModelManager
        self.models = ModelManager(self)
        self.store.recover()
        self.thread = threading.Thread(target=self._work, name="voice-queue", daemon=True)
        self.thread.start()

    def submit(self, title, model_name, segments, overrides=None, job_id=None, script=None, workflow_key=None):
        # Validation and enqueue must be atomic with model deletion/reconfiguration.
        with self.lock:
            if self.stopping.is_set():
                raise VoiceError("SERVICE_STOPPING", "服务正在停止，请重启后生成", stage="提交")
            if workflow_key and not job_id:
                try:
                    job_id = self.store.workflow_job(workflow_key)
                except ValueError as exc:
                    raise VoiceError("JOB_NOT_FOUND", str(exc), stage="工作流提交") from exc
            return self._submit(title, model_name, segments, overrides, job_id, script, workflow_key)

    def _submit(self, title, model_name, segments, overrides=None, job_id=None, script=None, workflow_key=None):
        if not segments or any(not s["text"].strip() for s in segments):
            raise VoiceError("EMPTY_TEXT", "文本不能为空", "检查字幕中的空白片段。", "提交")
        voice, _ = self.config.resolve(model_name, allow_missing_reference=overrides is not None and "reference_audio" in overrides)
        if overrides and "request_options" in overrides:
            changed_model = any(name in overrides and overrides[name] != voice.get(name) for name in ("family", "model_dir", "engine_executable"))
            previous = {} if changed_model else voice.get("request_options", {})
            overrides = {**overrides, "request_options": {**previous, **overrides["request_options"]}}
        base = voice
        voice = dict(voice, **(overrides or {}))
        check_voice(voice)
        info = model_ui(voice)
        # Capture the source preset before seed materialization; snapshots never follow later edits.
        from .schemas import VoiceConfig
        before, after = VoiceConfig(**base).model_dump(), VoiceConfig(**voice).model_dump()
        specs = info.get("generation_options", [])
        defaults = {s["name"]: s.get("default") for s in specs}
        for value in (before, after):
            value["request_options"] = {**defaults, **value.get("request_options", {})}
            value["voice_mode"] = selected_mode(value, info)
        changed_model = self.config.model_identity(base) != self.config.model_identity(voice)
        preset_used = not base.get("is_template", False) and not changed_model
        provenance = {"preset_used": preset_used, "preset_id": base["id"] if preset_used else None,
                      "preset_name": base["name"] if preset_used else None,
                      "preset_modified": preset_used and before != after,
                      "mode": selected_mode(voice, info)}
        if voice.get("request_options", {}).get("seed") == -1:
            # Persist the actual random seed so retry reuses it and a new take gets a new cache key.
            voice["request_options"] = {**voice["request_options"], "seed": secrets.randbelow(2147483648)}
        fingerprint = voice_fingerprint(voice)
        with self.lock:
            if job_id:
                existing = self.store.get(job_id)
                if existing is None:
                    raise VoiceError("JOB_NOT_FOUND", "当前作品不存在", stage="提交")
                if script is None:
                    script = existing.get("script")
                self.store.append(job_id, title, voice, fingerprint, segments, script, provenance, workflow=bool(workflow_key))
            else:
                job_id = self.store.create(title, voice, fingerprint, segments, script, provenance, workflow_key)
            self.wake.set()
        return job_id

    def cancel(self, job_id, generation=None):
        with self.lock:
            self.store.cancel(job_id, generation)
            for slot, active in self.active.items():
                if active["job_id"] == job_id and (generation is None or active["generation"] == generation):
                    active["cancelled"].set()
                    if slot in self.runners:
                        self.runners[slot].interrupt()

    def retry(self, job_id):
        with self.lock:
            job = self.store.get(job_id)
            for item in job["segments"]:
                if item["status"] in ("failed", "cancelled") and voice_fingerprint(item["voice"]) != item["fingerprint"]:
                    raise VoiceError("VOICE_VERSION_CHANGED", f"第 {item['idx']} 段的模型或参考音频已改变", "恢复原文件后重试，或按新配置重新生成。", "重试")
            self.store.retry(job_id)
            self.wake.set()

    def delete(self, job_id):
        with self.lock:
            if self.store.get(job_id) is None:
                raise VoiceError("JOB_NOT_FOUND", "任务不存在", stage="任务删除")
            self.cancel(job_id)
            self.store.delete(job_id)

    def restore(self, job_id):
        with self.lock:
            if self.store.get(job_id, include_deleted=True) is None:
                raise VoiceError("JOB_NOT_FOUND", "任务不存在", stage="任务恢复")
            self.store.restore(job_id)

    def status(self):
        with self.lock:
            return {"current": {"job_id": self.current[0], "index": self.current[1]} if self.current else None,
                    "active": [{"job_id": a["job_id"], "index": a["index"]} for a in self.active.values()],
                    "performance": self.config.performance(), "performance_notice": self.performance_notice,
                    "queue": sum(j["status"] in ("queued", "running") for j in self.store.list(10000))}

    def release_runners(self):
        for runner in self.runners.values():
            runner.interrupt()

    def set_performance(self, settings):
        with self.lock:
            if self.active:
                raise VoiceError("ENGINE_BUSY", "正在生成音频，请完成或取消后调整并发设置", stage="性能设置")
            self.release_runners()
            self.config.save_performance(settings)
            self.serial_jobs.clear()
            self.performance_notice = ""
            return self.config.performance()

    def _capacity(self, job, item):
        if not self.parallel_supported or not item.get("workflow") or item["voice"]["backend"] != "audio_cpp" or job["id"] in self.serial_jobs:
            return 1
        return self.config.performance()["workflow_workers"]

    def _runner(self, slot, capacity):
        if slot not in self.runners:
            root = self.root / "workers" / str(slot)
            for folder in ("logs", "temporary"):
                (root / folder).mkdir(parents=True, exist_ok=True)
            self.runners[slot] = self.runner_factory(root)
        runner = self.runners[slot]
        if isinstance(runner, UnifiedRunner):
            runner.native.cpu_threads = self.config.performance()["cpu_threads"] or min(8, max(1, (os.cpu_count() or 4) // capacity))
        return runner

    def generated_audio(self):
        result = self.store.generated_audio()
        for item in result:
            voice = item["voice"]
            item["model"] = Path(voice.get("model_dir") or "").name or ("Windows 系统声音" if voice.get("backend") == "windows_sapi" else voice.get("family", "未知模型"))
            mode = (item["provenance"] or {}).get("mode") or voice.get("voice_mode")
            if mode in (None, "auto"):
                mode = "builtin" if voice.get("backend") == "windows_sapi" or voice.get("family") == "kokoro_tts" else "clone" if voice.get("backend") == "index_tts" or voice.get("family") in ("index_tts2", "voxcpm2") else "auto"
            item["mode"] = MODES.get(mode, "自动模式（旧记录）")
            path = self.root / "audio" / item["audio"]
            item["audio_url"] = f"/audio/{item['audio']}"
            item["available"] = path.is_file()
            if item["duration"] is None and item["available"]:
                try:
                    item["duration"] = validate_wave(path)["duration"]
                except VoiceError:
                    pass
        return result

    def delete_generated_audio(self, ids=None, delete_all=False):
        with self.lock:
            records = self.store.generated_audio()
            wanted = {r["id"] for r in records} if delete_all else set(ids or [])
            unknown = wanted - {r["id"] for r in records}
            if unknown:
                raise VoiceError("AUDIO_NOT_FOUND", "部分音频已删除，请刷新列表后重试", stage="音频删除")
            groups = {}
            for record in records:
                groups.setdefault(record["audio"], []).append(record["id"])
            deleted, errors = [], []
            for filename, references in groups.items():
                selected = wanted.intersection(references)
                if not selected:
                    continue
                # The cache can serve several takes. Delete the file only after its last record.
                if selected == set(references):
                    if not re.fullmatch(r"[0-9a-f]{64}\.wav", filename):
                        errors.append("非法音频文件名，已跳过：" + filename)
                        continue
                    try:
                        path = self.root / "audio" / filename
                        if path.resolve().parent != (self.root / "audio").resolve():
                            raise OSError("音频路径超出管理目录")
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        errors.append(f"{filename}：{exc}")
                        continue
                    self.store.forget_cache(filename)
                self.store.erase_audio_records(selected)
                deleted.extend(sorted(selected))
            return {"deleted": deleted, "errors": errors}

    def _work(self):
        # Each lane owns its engine process. audio.cpp v0.7.4 serializes one model's
        # requests internally, so concurrent HTTP calls to one engine cannot speed it up.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="voice-inference") as pool:
            while not self.stopping.is_set():
                submitted = False
                with self.lock:
                    if not self.stopping.is_set():
                        cohort = next(iter(self.active.values()), None)
                        capacity = cohort["capacity"] if cohort else 4
                        if cohort and cohort["job_id"] in self.serial_jobs:
                            capacity = 1
                        if len(self.active) < capacity:
                            task = self.store.next(cohort["job_id"] if cohort else None,
                                                   admitted=lambda j, s: len(self.active) < self._capacity(j, s))
                            if task:
                                job, item = task
                                capacity = min(capacity, self._capacity(job, item))
                                # A fallback must release idle replicas before its retry.
                                if capacity == 1:
                                    for lane, runner in self.runners.items():
                                        if lane not in self.active and lane != 0:
                                            runner.interrupt()
                                slot = next(i for i in range(4) if i not in self.active)
                                cancelled = threading.Event()
                                self.active[slot] = {"job_id": job["id"], "index": item["idx"],
                                                     "generation": item["generation"], "capacity": capacity,
                                                     "cancelled": cancelled}
                                self.current = next((a["job_id"], a["index"]) for a in self.active.values())
                                pool.submit(self._execute, slot, job, item, cancelled, capacity)
                                submitted = True
                if not submitted:
                    self.wake.wait(.1)
                    self.wake.clear()

    def _execute(self, slot, job, item, cancelled, capacity):
        job_id, index = job["id"], item["idx"]
        voice, fingerprint = item["voice"], item["fingerprint"]
        temporary = self.root / "temporary" / f"{job_id}-{index}.wav"
        try:
            with self.lock:
                if cancelled.is_set() or self.stopping.is_set():
                    return
                runner = self._runner(slot, capacity)
            if voice_fingerprint(voice) != fingerprint:
                raise VoiceError("VOICE_VERSION_CHANGED", "排队期间模型或参考音频发生变化", "恢复原文件或按新配置提交任务。", "配置检查")
            key = hashlib.sha256((fingerprint + "\n" + item["text"]).encode("utf-8")).hexdigest()
            output = self.root / "audio" / f"{key}.wav"
            cached = False
            with self.lock:
                cache = self.store.cache_get(key)
                if cache:
                    try:
                        details = validate_wave(output)
                        cached = cache["checksum"] == file_digest(str(output))
                    except Exception:
                        cached = False
                if cached and not self.stopping.is_set() and not cancelled.is_set():
                    self.store.finish(job_id, index, audio=output.name, cached=True, duration=details["duration"])
                    return
            if not cached:
                runner.generate({"voice": voice, "fingerprint": fingerprint,
                                      "text": item["text"], "output": str(temporary),
                                      "job_id": job_id, "index": index}, cancelled)
                details = validate_wave(temporary)
                with self.lock:
                    if self.stopping.is_set() or cancelled.is_set():
                        return
                    os.replace(temporary, output)
                    self.store.cache_put(key, output, file_digest(str(output)))
                    self.store.finish(job_id, index, audio=output.name, cached=False, duration=details["duration"])
        except Exception as exc:
            error = describe_error(exc, "推理")
            error["log"] = f"/logs/{job_id}/{index}"
            log = {"job_id": job_id, "index": index, "text": item["text"], "timestamp": item["timestamp"],
                   "voice": voice, "error": error,
                   "traceback": getattr(exc, "engine_traceback", "") or traceback.format_exc()}
            atomic_json(self.root / "logs" / f"{job_id}-{index}.json", log)
            with self.lock:
                if not self.stopping.is_set() and not cancelled.is_set():
                    if error["code"] == "GPU_OUT_OF_MEMORY" and job.get("workflow_key") and self.active[slot]["capacity"] > 1 and (job_id, index) not in self.oom_retried:
                        self.serial_jobs.add(job_id)
                        self.oom_retried.add((job_id, index))
                        self.performance_notice = "批量并发遇到显存不足，当前任务已降为单实例，失败片段自动重试一次。"
                        self.store.requeue(job_id, index)
                    else:
                        self.store.finish(job_id, index, error=error)
        finally:
            temporary.unlink(missing_ok=True)
            with self.lock:
                self.active.pop(slot, None)
                self.current = next(((a["job_id"], a["index"]) for a in self.active.values()), None)
                self.wake.set()

    def close(self):
        with self.lock:
            self.stopping.set()
            for active in self.active.values():
                active["cancelled"].set()
            self.release_runners()
            self.wake.set()
        self.models.close()
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            raise RuntimeError("推理队列仍在退出中，不能关闭任务数据库")
        self.store.close()
        self.service_lock.close()
