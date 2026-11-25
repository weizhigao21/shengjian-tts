"""Prepare a private PCM WAV using the bundled decoder in an isolated process."""
import os
import shutil
import subprocess
import time

from ..errors import VoiceError
from ..runner import validate_wave
from ..runtime import audio_decode_command, project_root

# A video container is demultiplexed and its audio track decoded before recognition
# can start. Reading a two-hour screen recording is nothing like copying a short
# recording, so the video path gets its own, wider, deadline — the decoder is told the
# same limit one step behind so it reports the timeout instead of being killed.
DECODE_SECONDS = 120
VIDEO_DECODE_SECONDS = 300


def _reason(error):
    """The decoder's last line is the actionable one; the demuxer banner is not."""
    lines = [line.strip() for line in (error or "").splitlines() if line.strip()]
    return lines[-1] if lines else "解码器没有返回原因"


def import_wave(source, target, cancelled=None, video=False):
    seconds = VIDEO_DECODE_SECONDS if video else DECODE_SECONDS
    try:
        try:
            validate_wave(source)
        except VoiceError:
            environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1", QT_MEDIA_BACKEND="ffmpeg")
            process = subprocess.Popen(audio_decode_command(source, target, video=video), cwd=project_root(),
                                       env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                deadline = time.monotonic() + seconds
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise VoiceError("SERVICE_STOPPING", "音频导入已取消", stage="导入音频")
                    try:
                        _, error = process.communicate(timeout=.25)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            raise
                if process.returncode:
                    if video:
                        raise VoiceError("RECOGNITION_AUDIO_INVALID", "无法提取视频音轨：" + _reason(error),
                                         "请确认视频可以正常播放并包含音轨，或先用工具导出音频后再导入。", "导入音频")
                    raise VoiceError("RECOGNITION_AUDIO_INVALID", "无法解码音频：" + _reason(error),
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
        what = "视频音轨提取" if video else "音频解码"
        hint = "请确认视频可读取且包含音轨，或改用更短的视频后重试。" if video else "请确认文件可读取，或将文件转换为 PCM WAV 后重试。"
        raise VoiceError("RECOGNITION_AUDIO_INVALID", f"{what}未完成：{exc}", hint, "导入音频") from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise
