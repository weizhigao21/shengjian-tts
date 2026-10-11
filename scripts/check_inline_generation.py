"""Exercise a real model-file/timbre override without changing stored presets."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.manager import Manager
from voice_workbench.runner import validate_wave


def main():
    manager = Manager(ROOT / "artifacts" / "inline-model-check")
    try:
        voices = manager.config.voices()
        base = next(v for v in voices if v.get("family") == "index_tts2" and v["backend"] == "audio_cpp")
        target = next(v for v in voices if v.get("family") == "kokoro_tts" and v["backend"] == "audio_cpp")
        overrides = {"model_dir": target["model_dir"], "engine_executable": target["engine_executable"],
                     "family": "kokoro_tts", "voice_mode": "builtin", "native_voice": "zm_yunxi",
                     "language": "zh", "speed": 1.1, "emotion": "", "voice_description": "",
                     "reference_audio": "", "reference_text": ""}
        job_id = manager.submit("生成页模型配置验证", base["id"], [{"text": "生成页面现在可以直接配置模型和音色了。"}], overrides)
        print("job:", job_id, flush=True)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            job = manager.store.get(job_id)
            if job["status"] in ("done", "failed"):
                if job["status"] != "done":
                    print(json.dumps(job, ensure_ascii=True), flush=True)
                    return 1
                assert job["voice"]["model_dir"] == target["model_dir"]
                assert job["voice"]["native_voice"] == "zm_yunxi" and not job["voice"]["reference_audio"]
                assert next(v for v in manager.config.voices() if v["id"] == base["id"]) == base
                audio = manager.root / "audio" / job["segments"][0]["audio"]
                print("real_audio:", str(audio), validate_wave(audio), "cached:", job["segments"][0]["cached"], flush=True)
                return 0
            time.sleep(.25)
        raise RuntimeError("真实模型检查超时")
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
