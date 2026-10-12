from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
from .runtime import external_environment
import tempfile
import threading
import time

import httpx

from .config import atomic_json
from .errors import VoiceError
from .runner import ProcessRunner, check_voice
from .model_info import KOKORO_LANGUAGES, model_ui, selected_mode
from .generation_options import compatible_options
from .native_paths import NativePaths


def build_native_request(voice, text):
    info = model_ui({**voice, "backend": "audio_cpp"})
    options, _ = compatible_options(voice, info)
    # The UI's 0/-1 sentinel values mean "use the model default / random".
    if options.get("text_chunk_size") == 0:
        options.pop("text_chunk_size")
    if options.get("seed") == -1:
        options.pop("seed")
    request = {"text": text, "options": options}
    mode = selected_mode({**voice, "backend": "audio_cpp"}, info)
    if voice.get("reference_audio") and mode in ("clone", "continuation"):
        request["voice_ref"] = voice["reference_audio"]
    family = voice["family"]
    if family == "index_tts2":
        options.update(language=voice["language"], duration_factor=1 / voice["speed"])
        if voice["emotion"]:
            options.update(use_emotion_text=True, emotion_text=voice["emotion"], emotion_alpha=0.6)
    elif family == "kokoro_tts":
        for key in ("instruct", "reference_text", "emotion_text", "use_emotion_text", "emotion_alpha", "duration_factor"):
            options.pop(key, None)
        request["voice_id"] = voice["native_voice"]
        options["language"] = KOKORO_LANGUAGES.get(voice["native_voice"][0], voice["language"])
        options.update(speed=voice["speed"])
    elif family == "voxcpm2":
        options.setdefault("guidance_scale", 2)
        options.setdefault("num_inference_steps", 10)
        if voice.get("voice_description") and mode != "continuation":
            request["text"] = f"({voice['voice_description'].strip()})" + text
        if voice.get("reference_text") and mode in ("clone", "continuation"):
            request["reference_text"] = voice["reference_text"]
    elif family == "qwen3_tts":
        request["language"] = {"zh": "Chinese", "en": "English", "ja": "Japanese", "es": "Spanish", "ko": "Korean",
                               "de": "German", "fr": "French", "ru": "Russian", "pt": "Portuguese", "it": "Italian"}.get(voice["language"], voice["language"])
        if mode == "clone":
            options["x_vector_only_mode"] = not bool(voice.get("reference_text"))
        if mode == "builtin":
            options["speaker"] = voice["native_voice"]
        if voice.get("voice_description") and mode in ("design", "builtin"):
            options["instruct"] = voice["voice_description"]
        if voice.get("reference_text") and mode == "clone":
            options["reference_text"] = voice["reference_text"]
    return {"model": "speech", "request": request}


class AudioCppRunner:
    def __init__(self, root: Path, cpu_threads=None):
        self.root = Path(root)
        self.cpu_threads = cpu_threads
        self.lock = threading.RLock()
        self.process = None
        self.key = None
        self.endpoint = None
        self.log_handle = None
        self.native_paths = None

    def start(self, voice, cancelled):
        load = voice.get("load_options", {})
        # 同一模型切换参考声音时复用已加载的权重。
        identity = (voice["engine_executable"], voice["model_dir"], voice["family"], voice["device"], load,
                    Path(voice["model_dir"]).stat().st_mtime_ns, voice["revision"], self.cpu_threads)
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self.process and self.process.poll() is None and self.key == key:
                return self.process, self.endpoint
            self.interrupt()
            try:
                self.native_paths = NativePaths()
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                task = "vdes" if voice["family"] == "qwen3_tts" and selected_mode(voice) == "design" else "tts"
                native_model = self.native_paths.model(voice["model_dir"])
                entry = {"id": "speech", "family": voice["family"], "path": native_model, "task": task, "mode": "offline"}
                if load:
                    entry["load_options"] = load
                if voice["family"] == "voxcpm2":
                    entry["session_options"] = {"voxcpm2.audiovae_encoder_sample_capacity": 960000}
                config = {"host": "127.0.0.1", "port": port, "backend": voice["device"], "device": 0,
                          "threads": self.cpu_threads or min(8, max(1, (os.cpu_count() or 4) // 2)), "lazy_load": True,
                          "max_loaded_models": 1, "idle_unload_ms": 120000, "log_request_body": False,
                          "max_request_body_bytes": 1048576, "models": [entry]}
                config_path = self.root / "engine-server.json"
                atomic_json(config_path, config)
                native_config_path = self.native_paths.config(config)
                logs = self.root / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                self.log_handle = (logs / "audio-cpp.log").open("ab")
                executable = Path(voice["engine_executable"])
                with external_environment() as environment:
                    self.process = subprocess.Popen([str(executable), "--config", str(native_config_path), "--no-ui"],
                                                    cwd=executable.parent, stdout=self.log_handle, stderr=self.log_handle,
                                                    env=environment,
                                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.key = key
                self.endpoint = f"http://127.0.0.1:{port}"
                process, endpoint = self.process, self.endpoint
            except Exception:
                self.interrupt()
                raise
        deadline = time.monotonic() + min(voice["timeout"], 120)
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if cancelled.is_set():
                    self.interrupt()
                    raise VoiceError("CANCELLED", "任务已取消")
                if process.poll() is not None:
                    self.interrupt()
                    raise VoiceError("ENGINE_EXITED", f"统一引擎启动失败，退出码 {process.returncode}", "打开 audio-cpp.log 查看 DLL、后端或配置错误。", "引擎启动")
                try:
                    if client.get(endpoint + "/health").status_code == 200:
                        return process, endpoint
                except httpx.HTTPError:
                    pass
                cancelled.wait(0.2)
        self.interrupt()
        raise VoiceError("ENGINE_START_TIMEOUT", "统一引擎启动超时", "打开 audio-cpp.log 检查引擎启动过程。", "引擎启动")

    def generate(self, request, cancelled):
        voice = request["voice"]
        check_voice(voice)
        process, endpoint = self.start(voice, cancelled)
        try:
            # 部分原生 WAV 读取器不支持 Unicode 路径，暂存参考音频至 ASCII 临时路径。
            with tempfile.TemporaryDirectory(prefix="voice-workbench-") as staging:
                native_voice = dict(voice)
                if voice.get("reference_audio") and selected_mode(voice) in ("clone", "continuation"):
                    staged = Path(staging) / "reference.wav"
                    if not str(staged).isascii():
                        raise VoiceError("NATIVE_PATH_ENCODING", "系统临时目录含非 ASCII 字符", "设置 TMP 为可写的英文路径后重启程序。", "参考音频准备")
                    shutil.copy2(voice["reference_audio"], staged)
                    native_voice["reference_audio"] = str(staged)
                with httpx.Client(trust_env=False, timeout=voice["timeout"]) as client:
                    response = client.post(endpoint + "/v1/tasks/run", json=build_native_request(native_voice, request["text"]))
            if response.status_code != 200:
                raw = response.text[:1048576]
                if "out of memory" in raw.lower() or "insufficient_memory" in raw.lower():
                    error = VoiceError("GPU_OUT_OF_MEMORY", "统一引擎报告内存或显存不足",
                                       "释放其他模型或 GPU 任务后重试；原始响应保存在该片段日志。", "模型加载或推理")
                else:
                    error = VoiceError("ENGINE_HTTP_ERROR", f"统一引擎 HTTP {response.status_code}：{raw}",
                                       "检查片段日志中的原始响应和 audio-cpp.log；修正后仅重试失败片段。", "模型加载或推理")
                error.engine_traceback = raw
                raise error
            result = response.json()
            if not result.get("audio"):
                raise VoiceError("ENGINE_NO_AUDIO", "统一引擎返回成功但没有音频", "查看片段日志及引擎日志。", "音频接收")
            audio = base64.b64decode(result["audio"], validate=True)
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "任务已取消")
            Path(request["output"]).write_bytes(audio)
        except httpx.TimeoutException as exc:
            self.interrupt()
            raise VoiceError("INFERENCE_TIMEOUT", f"统一引擎推理超过 {voice['timeout']} 秒", "查看引擎日志，再调整超时或缩短文本。", "推理") from exc
        except Exception:
            self.interrupt()
            raise

    def interrupt(self):
        with self.lock:
            process, self.process = self.process, None
            self.key = None
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            if self.native_paths:
                self.native_paths.close()
                self.native_paths = None


class UnifiedRunner:
    def __init__(self, root):
        self.native = AudioCppRunner(root)
        self.python = ProcessRunner(root)

    def generate(self, request, cancelled):
        if request["voice"]["backend"] == "audio_cpp":
            self.python.interrupt()
            return self.native.generate(request, cancelled)
        self.native.interrupt()
        return self.python.generate(request, cancelled)

    def interrupt(self):
        self.native.interrupt()
        self.python.interrupt()
