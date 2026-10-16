"""Prepare a private PCM clip for the existing reference-library importer."""
import math
from pathlib import Path
import tempfile

from ..audio_editing.pcm import render_wave
from ..errors import VoiceError


def reference_audio_path(data_dir, filename):
    root = (Path(data_dir) / "recognition").resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise VoiceError("RECOGNITION_AUDIO_NOT_FOUND", "识别音频已不存在，请重新导入。", stage="保存参考声音")
    return path


def prepare_reference_clip(data_dir, filename, start, end):
    if not all(math.isfinite(t) for t in (start, end)) or start < 0 or not 1-1e-6 <= end - start <= 60+1e-6:
        raise VoiceError("REFERENCE_DURATION", "请选择 1–60 秒的连续识别片段；可按住 Shift 选择多行。", stage="保存参考声音")
    source = reference_audio_path(data_dir, filename)
    temporary = tempfile.TemporaryDirectory(prefix="shengjian-recognition-reference-")
    try:
        path = Path(temporary.name) / "reference.wav"
        render_wave(source, path, start=start, end=end)
        return temporary, path
    except BaseException:
        temporary.cleanup()
        raise
