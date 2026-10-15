"""Drive the shared native engine for transcription and forced alignment.

Mirrors the audio-editor adapter: reuse :class:`AudioCppRunner`'s process and
native-path handling, but declare both recognition models in one engine config so
a single process serves the ASR and the aligner in sequence. Audio travels in the
request body (multipart), so Unicode user paths never reach the native reader.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from ..audio_cpp import AudioCppRunner
from ..config import atomic_json
from ..errors import VoiceError
from ..native_paths import NativePaths
from ..runtime import external_environment

# Try the OpenAI-style alignment route first; fall back to the generic task route.
ALIGN_ENDPOINTS = ("/v1/audio/alignments", "/v1/tasks/run")


def _model_entry(voice, name, path):
    task = voice.get("task") or "asr"
    entry = {"id": name, "family": voice["family"], "path": path, "task": task, "mode": "offline"}
    load = voice.get("load_options") or {}
    if load:
        entry["load_options"] = load
    return entry


class RecognitionRunner(AudioCppRunner):
    """One engine process, two models: ``asr`` for text, ``align`` for timings."""

    def start(self, voices, cancelled):
        """Launch (or reuse) the engine with the ASR and aligner models resident."""
        identity = [(v["engine_executable"], v["model_dir"], v["family"], v["device"],
                     Path(v["model_dir"]).stat().st_mtime_ns, self.cpu_threads) for v in voices]
        key = hashlib.sha256(repr(identity).encode()).hexdigest()
        with self.lock:
            if self.process and self.process.poll() is None and self.key == key:
                return self.process, self.endpoint
            self.interrupt()
            try:
                self.native_paths = NativePaths()
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                entries = [_model_entry(voice, name, self.native_paths.model(voice["model_dir"]))
                           for voice, name in zip(voices, ("asr", "align"))]
                config = {"host": "127.0.0.1", "port": port, "backend": voices[0]["device"], "device": 0,
                          "threads": self.cpu_threads or min(8, max(1, (os.cpu_count() or 4) // 2)),
                          "lazy_load": True, "max_loaded_models": 2, "idle_unload_ms": 120000,
                          "log_request_body": False, "max_request_body_bytes": 104857600,
                          "models": entries}
                native_config_path = self.native_paths.config(config)
                atomic_json(self.root / "engine-server.json", config)
                logs = self.root / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                self.log_handle = (logs / "audio-cpp.log").open("ab")
                executable = Path(voices[0]["engine_executable"])
                with external_environment() as environment:
                    self.process = subprocess.Popen(
                        [str(executable), "--config", str(native_config_path), "--no-ui"],
                        cwd=executable.parent, stdout=self.log_handle, stderr=self.log_handle, env=environment,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.key = key
                self.endpoint = f"http://127.0.0.1:{port}"
                process, endpoint = self.process, self.endpoint
            except Exception:
                self.interrupt()
                raise
        deadline = time.monotonic() + min(voices[0]["timeout"], 180)
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if cancelled.is_set():
                    self.interrupt()
                    raise VoiceError("CANCELLED", "识别已取消")
                if process.poll() is not None:
                    self.interrupt()
                    raise VoiceError("ENGINE_EXITED", f"统一引擎启动失败，退出码 {process.returncode}",
                                     "打开 audio-cpp.log 查看 DLL、后端或配置错误。", "引擎启动")
                try:
                    if client.get(endpoint + "/health").status_code == 200:
                        return process, endpoint
                except httpx.HTTPError:
                    pass
                cancelled.wait(0.2)
        self.interrupt()
        raise VoiceError("ENGINE_START_TIMEOUT", "统一引擎启动超时", "打开 audio-cpp.log 检查引擎启动过程。", "引擎启动")

    def transcribe(self, voices, audio, language, cancelled, timeout=900):
        """Return the engine's transcript payload for one audio file."""
        _, endpoint = self.start(voices, cancelled)
        fields = {"model": "asr"}
        if language and language != "auto":
            fields["language"] = language
        try:
            with open(audio, "rb") as handle:
                files = {"file": (Path(audio).name, handle, "audio/wav")}
                with httpx.Client(trust_env=False, timeout=timeout) as client:
                    response = client.post(endpoint + "/v1/audio/transcriptions", files=files, data=fields)
            return self._payload(response, "识别")
        except httpx.TimeoutException as exc:
            self.interrupt()
            raise VoiceError("INFERENCE_TIMEOUT", f"识别超过 {timeout} 秒", "缩短音频或分段后重试。", "语音识别") from exc

    def align(self, voices, audio, text, language, cancelled, timeout=900):
        """Return word/segment timings for an exact transcript, or ``None`` if unsupported.

        The aligner is reachable two ways; both were confirmed against the engine:
        ``/v1/audio/alignments`` (OpenAI-style multipart, preferred) and
        ``/v1/tasks/run`` with ``request`` carrying ``audio``/``text``/``language``.
        The returned word times must be read from ``start_sample``/``end_sample``;
        see ``lines.ALIGNER_SAMPLE_RATE``.
        """
        _, endpoint = self.start(voices, cancelled)
        fields = {"model": "align", "text": text}
        if language and language != "auto":
            fields["language"] = language
        last = ""
        try:
            for position, route in enumerate(ALIGN_ENDPOINTS):
                with httpx.Client(trust_env=False, timeout=timeout) as client:
                    if route == "/v1/audio/alignments":
                        with open(audio, "rb") as handle:
                            files = {"file": (Path(audio).name, handle, "audio/wav")}
                            response = client.post(endpoint + route, files=files, data=fields)
                    else:
                        # The native reader needs an ASCII path; the audio copy may live
                        # under a Unicode data directory.
                        with tempfile.TemporaryDirectory(prefix="shengjian-align-") as folder:
                            if not folder.isascii():
                                raise VoiceError("NATIVE_PATH_ENCODING", "系统临时目录含非 ASCII 字符",
                                                 "设置 TMP 为可写的英文路径后重启程序。", "字幕对齐")
                            staged = Path(folder) / (Path(audio).name if Path(audio).name.isascii() else "align-input.wav")
                            shutil.copyfile(audio, staged)
                            request = {"audio": str(staged), "text": text}
                            if language and language != "auto":
                                request["language"] = language
                            response = client.post(endpoint + route, json={"model": "align", "request": request})
                if response.status_code == 200:
                    return response.json()
                last = response.text[:2000]
                if position == len(ALIGN_ENDPOINTS) - 1:
                    break
            raise VoiceError("ALIGN_FAILED", f"对齐未返回结果：{last}",
                             "确认已下载 Qwen3 强制对齐器模型；对齐器缺失时无法给出精确时间。", "字幕对齐")
        except httpx.TimeoutException as exc:
            self.interrupt()
            raise VoiceError("INFERENCE_TIMEOUT", f"对齐超过 {timeout} 秒", "缩短音频后重试。", "字幕对齐") from exc

    @staticmethod
    def _payload(response, stage):
        if response.status_code != 200:
            raw = response.text[:1048576]
            lower = raw.lower()
            code = "GPU_OUT_OF_MEMORY" if "out of memory" in lower or "insufficient_memory" in lower else "ENGINE_HTTP_ERROR"
            raise VoiceError(code, f"统一引擎 HTTP {response.status_code}：{raw}",
                             "检查片段日志中的原始响应和 audio-cpp.log。", stage)
        return response.json()
