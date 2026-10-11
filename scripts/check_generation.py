"""使用已配置的真实后端执行短句生成；不会把测试后端伪装成模型。"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voice_workbench.manager import Manager
from voice_workbench.runner import validate_wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default="index-2.5-q8-voice1")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    manager = Manager(args.data_dir)
    try:
        manager.config.import_yovoice()
        job_id = manager.submit("真实语音生成验证", args.voice, [{"text": "你好，这是本地配音工作台的语音测试。", "timestamp": "00:00.00"}])
        print("job:", job_id, flush=True)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            job = manager.store.get(job_id)
            if job["status"] in ("done", "failed"):
                print(json.dumps(job, ensure_ascii=True), flush=True)
                if job["status"] != "done":
                    return 1
                audio = manager.root / "audio" / job["segments"][0]["audio"]
                print("audio:", str(audio), validate_wave(audio), flush=True)
                return 0
            time.sleep(0.5)
        print("检查超时，任务在管理界面中保留。", flush=True)
        return 2
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
