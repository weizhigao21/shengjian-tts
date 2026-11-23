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
# How long a worker keeps its interpreter after its weights are gone. Rebuilding
# the process costs 8-50 s of interpreter and library import; loading the weights
# costs 5-8 s. Throwing both away at the first idle minute threw away the
# expensive half, so the process outlives the model by this much.
PROCESS_GRACE = 300


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
        self.unloaded = False
        self.busy = False
        self.spawned_at = None
        # Set when a process is spawned, cleared by its first answer. `loaded`
        # cannot serve here: releasing VRAM between jobs also clears it, yet the
        # interpreter that paid the import is still the same one.
        self.first_request = True
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
            # Stamped here so the worker's own boot stamp can be measured against
            # it: what lies between is the interpreter, and nothing else can see it.
            self.spawned_at = time.time()
            self.key = signature
            self.unloaded = False
            self.first_request = True
            self.answers = queue.Queue()
            threading.Thread(target=self.read, args=(self.process, self.answers), daemon=True).start()
            return self.process, self.answers

    def generate(self, settings, signature, payload, cancelled, timeout):
        if cancelled.is_set():
            raise VoiceError("CANCELLED", "任务已取消")
        # Two different clocks bound this call: `batch_seconds` in the caller is
        # measured with perf_counter from before start(), `roundtrip_seconds` with
        # monotonic from after it. The difference kept showing up as a second or
        # two nobody could name, so both ends of that window are stamped here.
        entered = time.monotonic()
        process, answers = self.start(settings, signature)
        after_start = time.monotonic()
        # Read before the request goes out: the interpreter cost belongs to the
        # first batch this process ever runs, and to nothing after that.
        first = self.first_request
        items = payload.get("items") or []
        started, frames = time.monotonic(), 0
        ceiling = started + timeout
        deadline = started + batch_budget(timeout, self.loaded)
        stall = batch_stall(timeout)
        with self.lock:
            # Held only for the flag: an idle release must never write to the
            # same pipe a batch is about to use.
            self.busy = True
        try:
            # `root` rides along so the worker can keep its voice-prompt cache
            # beside the rest of the data instead of guessing at a location.
            process.stdin.write(json.dumps({"settings": settings.model_dump(), "root": str(self.root),
                                            **payload}, ensure_ascii=False) + "\n")
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
                if answer.get("unloaded") is not None:
                    # The rest request shares this pipe; its reply is never a batch.
                    continue
                if not answer.get("ok"):
                    error = VoiceError("BATCH_BACKEND_FAILED", answer.get("message", "批量推理失败"), stage="批量推理")
                    error.engine_traceback = answer.get("traceback", "")
                    raise error
                self.last_used = time.monotonic()
                self.loaded = True
                self.unloaded = False
                self.first_request = False
                # Account for the whole round trip, not just the part the worker
                # can see: a batch that "took 5.5 s" while the model reported 4.5
                # has a second hiding somewhere, and it has to show up.
                answer["roundtrip_seconds"] = self.last_used - started
                # `call_seconds` spans start() and the pipe, `start_seconds` isolates
                # the part spent holding the worker lock before the request is even
                # written. A batch whose round trip is 3 s but whose call is 4.5 s is
                # waiting here, not in the model.
                answer["call_seconds"] = self.last_used - entered
                answer["start_seconds"] = max(0.0, after_start - entered)
                server = float(answer.get("server_seconds") or 0.0)
                entered = answer.get("entered_at")
                if entered is not None:
                    if first and self.spawned_at:
                        # Interpreter start plus module import: the worker's own
                        # stamp minus the moment Popen returned.
                        answer["spawn_seconds"] = max(0.0, entered - self.spawned_at)
                    else:
                        # A warm worker already paid it. Charging it again per
                        # batch is what used to flatten the leftover to zero and
                        # hide the seconds this field exists to find.
                        answer["spawn_seconds"] = 0.0
                    answer["ipc_seconds"] = max(0.0, answer["roundtrip_seconds"]
                                                - answer["spawn_seconds"] - server)
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
        finally:
            with self.lock:
                self.busy = False

    def release(self, idle, unload_after):
        """Rest in two steps: drop the weights, then the process.

        Measured on this machine: a cold worker spends 8-50 s importing torch and
        the SDK before it reads a request, then 5-8 s loading weights. Killing it
        at the first idle minute paid the expensive half again for nothing, so the
        weights go back to the card first and the interpreter stays resident.
        """
        with self.lock:
            process = self.process
            if self.busy or process is None or process.poll() is not None:
                return False
            if idle >= unload_after + PROCESS_GRACE:
                self.interrupt()
                return True
            if self.unloaded:
                return False
            try:
                process.stdin.write(json.dumps({"unload": True}) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                self.interrupt()
                return False
            self.unloaded = True
            # The next batch has no warm weights to rely on until it loads them.
            self.loaded = False
            return True

    def interrupt(self):
        with self.lock:
            process, self.process = self.process, None
            self.key = None
            self.loaded = False
            self.unloaded = False
            self.spawned_at = None
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
