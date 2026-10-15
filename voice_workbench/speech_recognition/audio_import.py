"""Prepare a private PCM WAV using the bundled decoder in an isolated process."""
import os
import shutil
import subprocess
import time

from ..errors import VoiceError
from ..runner import validate_wave
from ..runtime import audio_decode_command, project_root


def import_wave(source, target, cancelled=None):
    try:
        try:
            validate_wave(source)
        except VoiceError:
            environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1", QT_MEDIA_BACKEND="ffmpeg")
            process = subprocess.Popen(audio_decode_command(source, target), cwd=project_root(), env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                deadline = time.monotonic() + 120
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise VoiceError("SERVICE_STOPPING", "服务正在停止，音频导入已取消", stage="导入音频")
                    try:
                        _, error = process.communicate(timeout=.25)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            raise
                if process.returncode:
                    raise VoiceError("RECOGNITION_AUDIO_INVALID", "无法解码音频：" + error[-2000:],
                                     "请确认音频未损坏，或将文件转换为 PCM WAV 后重试。", "导入音频")
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        else:
            shutil.copyfile(source, target)
        return validate_wave(target)
    except (OSError, subprocess.TimeoutExpired) as exc:
        target.unlink(missing_ok=True)
        raise VoiceError("RECOGNITION_AUDIO_INVALID", f"音频解码未完成：{exc}",
                         "请确认文件可读取，或将文件转换为 PCM WAV 后重试。", "导入音频") from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise
