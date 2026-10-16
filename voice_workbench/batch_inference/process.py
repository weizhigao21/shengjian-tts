"""A single persistent external Python worker, with cancellable JSON-line IPC."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from ..errors import VoiceError
from ..runtime import external_environment, resource_root


class BatchProcess:
    def __init__(self, root):
        self.root = Path(root)
        self.process = None
        self.key = None
        self.log = None
        self.loaded = False
        self.lock = threading.RLock()
        self.last_used = time.monotonic()

    @staticmethod
    def read(process, answers):
        try:
            while True:
                line = process.stdout.readline(1048577)
                if not line:
                    break
                if len(line) > 1048576:
                    raise ValueError("批量后端响应过大")
                answers.put(json.loads(line))
        except Exception as exc:
            answers.put({"ok": False, "message": str(exc)})
        finally:
            answers.put({"ok": False, "message": "批量后端进程已退出"})

    def start(self, settings, signature):
        with self.lock:
            if self.process and self.process.poll() is None and self.key == signature:
                return self.process, self.answers
            self.interrupt()
            (self.root / "logs").mkdir(parents=True, exist_ok=True)
            self.log = (self.root / "logs/qwen-batch.log").open("a", encoding="utf-8")
            worker = resource_root() / "voice_workbench/batch_inference/qwen_worker.py"
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1",
                       TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
            with external_environment(env) as environment:
                self.process = subprocess.Popen([settings.python, "-u", str(worker)], cwd=worker.parent,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True,
                    encoding="utf-8", env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.key = signature
            self.answers = queue.Queue()
            threading.Thread(target=self.read, args=(self.process, self.answers), daemon=True).start()
            return self.process, self.answers

    def generate(self, settings, signature, payload, cancelled, timeout):
        if cancelled.is_set():
            raise VoiceError("CANCELLED", "任务已取消")
        process, answers = self.start(settings, signature)
        try:
            process.stdin.write(json.dumps({"settings": settings.model_dump(), **payload}, ensure_ascii=False) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancelled.is_set():
                    raise VoiceError("CANCELLED", "任务已取消")
                try:
                    answer = answers.get(timeout=.1)
                except queue.Empty:
                    continue
                if not answer.get("ok"):
                    error = VoiceError("BATCH_BACKEND_FAILED", answer.get("message", "批量推理失败"), stage="批量推理")
                    error.engine_traceback = answer.get("traceback", "")
                    raise error
                self.last_used = time.monotonic()
                self.loaded = True
                return answer
            raise VoiceError("INFERENCE_TIMEOUT", "批量后端推理超时", stage="批量推理")
        except BaseException:
            self.interrupt()
            raise

    def interrupt(self):
        with self.lock:
            process, self.process = self.process, None
            self.key = None
            self.loaded = False
            if process:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout):
                    if stream:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if self.log:
                self.log.close()
                self.log = None
