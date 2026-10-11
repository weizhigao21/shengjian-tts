"""Compare isolated native replicas; no user tasks, settings or models are changed."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.audio_cpp import AudioCppRunner
from voice_workbench.config import atomic_json
from voice_workbench.runner import validate_wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default="voxcpm2")
    parser.add_argument("--workers", default="1,2")
    parser.add_argument("--cpu-threads", type=int, default=4)
    args = parser.parse_args()
    value = json.loads((ROOT / "data/config.json").read_text(encoding="utf-8"))
    voice = next(v for v in value["voices"] if v.get("family") == args.family)
    voice = {**voice, "request_options": {**voice.get("request_options", {}), "seed": 1234}}
    output = ROOT / "artifacts/batch-performance" / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    texts = ["今天我们一起测试双语工作流的批量配音。", "每个字幕片段都应该生成对应的声音。",
             "完成的音频会按照原来的顺序保存。", "现在可以继续处理下一批字幕文本。"]
    report = {"model": voice["model_dir"], "family": args.family, "runs": []}
    for count in map(int, args.workers.split(",")):
        runners = [AudioCppRunner(output / f"workers-{count}" / str(i), cpu_threads=args.cpu_threads) for i in range(count)]
        cancelled = threading.Event()
        def generate(i, text):
            path = output / f"{count}-{i}.wav"
            runners[i % count].generate({"voice": voice, "text": text, "output": str(path)}, cancelled)
            return validate_wave(path)["duration"]
        try:
            # Keep model load and first graph construction outside the measured run.
            for i in range(count):
                generate(i, "测试声音。")
            before = time.monotonic()
            with ThreadPoolExecutor(max_workers=count) as pool:
                # One ordered lane per replica, so a runner never receives simultaneous requests.
                lanes = [pool.submit(lambda lane: [generate(i + count, texts[i]) for i in range(lane, len(texts), count)], lane)
                         for lane in range(count)]
                durations = [duration for lane in lanes for duration in lane.result()]
            elapsed = time.monotonic() - before
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
            result = {"workers": count, "cpu_threads_per_worker": args.cpu_threads, "seconds": elapsed,
                      "audio_seconds": sum(durations), "memory_after": gpu}
            report["runs"].append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:
            report["runs"].append({"workers": count, "error": str(exc)})
            print(str(exc), flush=True)
        finally:
            for runner in runners:
                runner.interrupt()
    atomic_json(output / "report.json", report)
    print(output / "report.json", flush=True)


if __name__ == "__main__":
    main()
