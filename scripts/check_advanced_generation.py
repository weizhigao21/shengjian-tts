"""用已安装模型验证高级参数；所有验证任务与输出都写入 artifacts。"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.manager import Manager
from voice_workbench.runner import validate_wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", choices=["voxcpm2", "kokoro_tts"], default="voxcpm2")
    args = parser.parse_args()
    manager = Manager(ROOT / "artifacts" / "advanced-generation-check")
    try:
        voice = next(v for v in manager.config.voices() if v["backend"] == "audio_cpp" and v["family"] == args.family)
        if args.family == "voxcpm2":
            options = {"guidance_scale": 2.4, "num_inference_steps": 6, "text_chunk_size": 16, "text_chunk_mode": "tag_aware",
                       "max_tokens": 256, "min_tokens": 2, "voxcpm2.chunk_strategy": "continuation", "retry_badcase": False, "seed": 37}
            overrides = {"voice_mode": "design", "voice_description": "温柔、自然的女声", "reference_audio": "", "reference_text": "", "request_options": options}
        else:
            options = {"seed": 37, "text_chunk_size": 64}
            overrides = {"request_options": options}
        job_id = manager.submit("高级参数真实生成验证", voice["id"], [{"text": "你好，这是高级参数测试。我们正在使用可配置的推理参数。"}], overrides)
        print("job:", job_id, "options:", json.dumps(options), flush=True)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            job = manager.store.get(job_id)
            if job["status"] in ("done", "failed"):
                if job["status"] == "failed":
                    print(json.dumps(job["segments"][0]["error"], ensure_ascii=True), flush=True)
                    return 1
                assert job["voice"]["request_options"] == options
                output = manager.root / "audio" / job["segments"][0]["audio"]
                print("audio:", output, validate_wave(output), "cached:", job["segments"][0]["cached"], flush=True)
                return 0
            time.sleep(.25)
        raise RuntimeError("高级参数生成验证超时")
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
