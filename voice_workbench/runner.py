from __future__ import annotations

import json
from contextlib import nullcontext
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import wave

from .config import PROJECT_ROOT
from .errors import VoiceError
from .model_info import model_ui, validate_capabilities
from .runtime import engine_command, external_environment, frozen
from .index_emotion import emotion_mode, validate_emotion


def validate_wave(path: Path):
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnframes() == 0 or not audio.readframes(min(audio.getnframes(), 4096)):
                raise ValueError("音频为空")
            return {"duration": audio.getnframes() / audio.getframerate(), "sample_rate": audio.getframerate()}
    except Exception as exc:
        raise VoiceError("INVALID_AUDIO", f"生成的 WAV 无法读取：{exc}", "查看片段日志后重试；无效文件不会写入缓存。", "音频验证") from exc


def check_voice(voice):
    is_index = voice["backend"] == "index_tts" or voice["backend"] == "audio_cpp" and voice.get("family") == "index_tts2"
    if is_index:
        try:
            mode = validate_emotion(voice)
        except ValueError as exc:
            raise VoiceError("INVALID_EMOTION", str(exc), "调整表达方式或情绪参数。", "配置检查") from exc
        if mode == "reference":
            path = Path(voice["emotion_reference_audio"])
            if not path.is_file():
                raise VoiceError("FILE_MISSING", "演绎参考音频不存在", "重新选择演绎参考 WAV。", "配置检查")
            duration = validate_wave(path)["duration"]
            if not 1 <= duration <= 60:
                raise VoiceError("REFERENCE_DURATION", "演绎参考需要 1–60 秒的 WAV", stage="配置检查")
    elif emotion_mode(voice) != "speaker":
        raise VoiceError("UNSUPPORTED_EMOTION", "该模型不支持 IndexTTS 表达方式", "选择 IndexTTS，或恢复跟随音色。", "配置检查")
    if (not frozen() or voice["backend"] == "index_tts") and not Path(voice["python"]).is_file():
        raise VoiceError("PYTHON_MISSING", f"Python 不存在：{voice['python']}", "选择模型环境里的 python.exe。", "配置检查")
    if voice["backend"] == "audio_cpp":
        for path in (Path(voice.get("engine_executable") or ""), Path(voice.get("model_dir") or "")):
            if not path.is_file():
                raise VoiceError("FILE_MISSING", f"统一引擎或模型不存在：{path}", "选择 audiocpp_server.exe 和兼容 GGUF 文件。", "配置检查")
        with Path(voice["model_dir"]).open("rb") as model:
            if model.read(4) != b"GGUF":
                raise VoiceError("INVALID_MODEL", "统一引擎适配器需要兼容的 GGUF 模型", "选择 audio.cpp 对应模型家族的 GGUF。", "配置检查")
        info = model_ui(voice)
        mode = validate_capabilities(voice, info)
        if mode in ("clone", "continuation"):
            path = Path(voice.get("reference_audio") or "")
            if not path.is_file():
                raise VoiceError("REFERENCE_REQUIRED", "该模型需要参考音频", "在生成设置中选择或上传参考 WAV。", "配置检查")
            duration = validate_wave(path)["duration"]
            if not 1 <= duration <= 60:
                raise VoiceError("REFERENCE_DURATION", f"参考音频长度为 {duration:.1f} 秒", "请选择 1–60 秒的参考 WAV。", "配置检查")
    elif voice["backend"] == "index_tts":
        module = "infer_v2.py" if voice["index_version"] == "2" else "infer_v2_5.py"
        for path in (Path(voice["repo_dir"]) / "indextts" / module,
                     Path(voice["model_dir"]) / "config.yaml", Path(voice["reference_audio"])):
            if not path.is_file():
                raise VoiceError("FILE_MISSING", f"文件不存在：{path}", "检查源码、模型及参考音频路径。", "配置检查")
        if voice["index_version"] == "2" and voice["language"] not in ("zh", "en"):
            raise VoiceError("UNSUPPORTED_LANGUAGE", "IndexTTS 2 只支持中文和英文", "选择中文/英文或切换到 2.5。", "配置检查")
        if voice["index_version"] == "2" and voice["speed"] != 1:
            raise VoiceError("UNSUPPORTED_SPEED", "IndexTTS 2 适配器暂不提供语速控制", "将语速设为 1，或切换到 2.5。", "配置检查")
    elif voice["language"] not in ("zh", "en") or voice.get("emotion"):
        raise VoiceError("UNSUPPORTED_STYLE", "Windows 系统声音只提供中文/英文及基础语速设置", "高级情绪和声音克隆请使用 IndexTTS。", "配置检查")


class ProcessRunner:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process = None
        self.key = None
        self.answers = None
        self.log_handle = None

    @staticmethod
    def _read(process, answers):
        try:
            for line in process.stdout:
                try:
                    answers.put(json.loads(line))
                except ValueError:
                    # 只有协议 JSON 会成为响应，第三方模型的额外输出不能损坏协议。
                    continue
        finally:
            answers.put({"ok": False, "message": f"推理进程已退出（退出码 {process.poll()}）"})

    def _start(self, voice, fingerprint):
        with self.lock:
            if self.process and self.process.poll() is None and self.key == fingerprint:
                return self.process, self.answers
            self.interrupt()
            self.log_handle = (self.root / "logs" / "engine.log").open("a", encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            command = engine_command(voice)
            context = external_environment(env) if voice["backend"] == "index_tts" else nullcontext(env)
            with context as environment:
                self.process = subprocess.Popen(
                    command, cwd=voice.get("repo_dir") or str(PROJECT_ROOT), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=self.log_handle, text=True, encoding="utf-8", env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.key = fingerprint
            self.answers = queue.Queue()
            threading.Thread(target=self._read, args=(self.process, self.answers), daemon=True).start()
            return self.process, self.answers

    def generate(self, request, cancelled):
        voice = request["voice"]
        check_voice(voice)
        process, answers = self._start(voice, request["fingerprint"])
        if cancelled.is_set():
            raise VoiceError("CANCELLED", "任务已取消")
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + voice["timeout"]
        while time.monotonic() < deadline:
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "任务已取消")
            try:
                answer = answers.get(timeout=0.2)
            except queue.Empty:
                continue
            if answer["ok"]:
                return
            exc = RuntimeError(answer["message"])
            exc.engine_traceback = answer.get("traceback", "")
            # 出错后释放模型/显存，下一次重试使用干净的进程。
            self.interrupt()
            raise exc
        self.interrupt()
        raise VoiceError("INFERENCE_TIMEOUT", f"片段推理超过 {voice['timeout']} 秒", "检查引擎日志；确认环境后调整超时或缩短文本。", "推理")

    def interrupt(self):
        with self.lock:
            process = self.process
            self.process = None
            self.key = None
            if process and process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                else:
                    process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process:
                if process.stdin:
                    process.stdin.close()
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
