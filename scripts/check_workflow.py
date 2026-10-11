"""用已准备的工作流适配器执行真实生成→下载→原混音器验证。"""
import importlib
import json
import logging
from pathlib import Path
import sys
import time
import types

import requests

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = Path(r"G:\code\双语音声工作流")


class Cache:
    def get_cached_audio(self, *args):
        return None

    def save_audio_cache(self, *args, **kwargs):
        pass


def main():
    package = types.ModuleType("prepared_steps")
    package.__path__ = [str(ROOT / "integration" / "prepared"), str(ROOT / "integration"), str(WORKFLOW / "src" / "steps")]
    sys.modules[package.__name__] = package
    logger_module = types.ModuleType("prepared_steps.tts_logger")
    logger_module.logger = logging.getLogger("workflow-validation")
    sys.modules[logger_module.__name__] = logger_module
    utils = importlib.import_module("prepared_steps.tts_utils")
    session = requests.Session()
    session.trust_env = False
    utils._get_session = lambda: session
    voices = session.get("http://127.0.0.1:8765/voices", timeout=5).json()
    voice = next(v for v in voices if v["id"] == "index-2.5-q8-voice1")
    output = ROOT / "artifacts" / "workflow-check"
    segments = output / "segments"
    segments.mkdir(parents=True, exist_ok=True)
    tasks = [(1, "00:00:00.00", "你好，这是本地配音工作台的语音测试。", str(segments), 0),
             (2, "00:00:05.00", "欢迎使用双语语音工作流。", str(segments), 0)]
    start = time.monotonic()
    results = utils.tts_bulk_task(tasks, "http://127.0.0.1:8765", voice["model_name"], Cache(), voice["model_tag"])
    print("generation:", results, "elapsed:", round(time.monotonic() - start, 2), flush=True)
    assert all(ok for ok, _ in results), results
    sys.path.insert(0, str(WORKFLOW))
    from src.task_manager import TaskInfo
    from src.steps.step3_mixer import mix_single_task
    from pydub import AudioSegment
    source = output / "source.wav"
    (AudioSegment.from_wav(voice["reference_audio"]) * 4).export(str(source), format="wav")
    task = TaskInfo("workflow-check", str(source), "source", str(output / "workspace"))
    task.step2_output = str(segments)
    config = types.SimpleNamespace(mixer_cfg={"output_format": "wav", "audio_sample_rate": 44100,
        "audio_channels": 2, "wav_bit_depth": 16, "channel_detect": False, "auto_volume": "off",
        "skip_existing": False, "export_dir": str(output / "mixed"), "streaming_threshold_minutes": 20,
        "write_metadata": False})
    ok, path = mix_single_task(task, config, log_callback=lambda msg: print(msg, flush=True))
    assert ok, path
    mixed = AudioSegment.from_wav(path)
    assert len(mixed) > 5000 and mixed.rms > 0
    report = {"voice": voice["id"], "backend": voice["backend"], "tts_results": results,
              "mixed_file": path, "duration_ms": len(mixed), "sample_rate": mixed.frame_rate,
              "channels": mixed.channels, "nonzero_audio": mixed.rms > 0}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("workflow verified:", json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
