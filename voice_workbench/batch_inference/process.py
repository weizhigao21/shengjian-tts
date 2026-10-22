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


FIRST_FRAME = 45
COLD_ALLOWANCE = 90
STALL_SECONDS = 90


def batch_budget(timeout, loaded=True):
    """How long a batch may go before its first frame arrives.

    A total-time budget cannot tell a slow batch from a wedged one. Measured on
    the same eight lines, the worker moves between 0.15 s and 1.15 s per frame
    depending on GPU clocks (825 vs 1900 MHz) and whether anything else holds
    VRAM on an 11 GiB card that the batch already fills to 10.4 GiB. So a budget
    guessed from the text declared a working batch stalled, and the retry went to
    the native engine, which renders those lines one at a time -- slower than the
    wait it was meant to avoid.

    The worker now reports every frame, so only the two stretches that produce no
    frames need a bound: starting the worker, and starting to speak.
    """
    return min(timeout, FIRST_FRAME if loaded else FIRST_FRAME + COLD_ALLOWANCE)


def batch_stall(timeout):
    """How long a batch may go without a frame once it has started producing them."""
    return min(timeout, STALL_SECONDS)


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
        items = payload.get("items") or []
        started, frames = time.monotonic(), 0
        ceiling = started + timeout
        deadline = started + batch_budget(timeout, self.loaded)
        stall = batch_stall(timeout)
        try:
            process.stdin.write(json.dumps({"settings": settings.model_dump(), **payload}, ensure_ascii=False) + "\n")
            process.stdin.flush()
            while time.monotonic() < deadline:
                if cancelled.is_set():
                    raise VoiceError("CANCELLED", "任务已取消")
                try:
                    answer = answers.get(timeout=.1)
                except queue.Empty:
                    continue
                if answer.get("progress"):
                    # A frame landed: judge the worker by its pace, not by the clock.
                    frames = answer["progress"]
                    deadline = min(ceiling, time.monotonic() + stall)
                    continue
                if not answer.get("ok"):
                    error = VoiceError("BATCH_BACKEND_FAILED", answer.get("message", "批量推理失败"), stage="批量推理")
                    error.engine_traceback = answer.get("traceback", "")
                    raise error
                self.last_used = time.monotonic()
                self.loaded = True
                return answer
            waited = time.monotonic() - started
            raise VoiceError("INFERENCE_TIMEOUT", (
                f"批量推理超时：{len(items)} 条已渲染 {frames} 帧后 {waited:.0f} 秒没有任何进展，已缩小批量重试"
                if frames else
                f"批量推理超时：{len(items)} 条等待超过 {waited:.0f} 秒仍未开始出帧，已缩小批量重试"),
                stage="批量推理")
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
