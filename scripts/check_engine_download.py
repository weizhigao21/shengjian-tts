"""Download/resume the official CPU engine and run it with isolated configuration."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.config import atomic_json
from voice_workbench.manager import Manager
from voice_workbench.runner import validate_wave


def main():
    folder = ROOT / "artifacts" / "engine-download-check" / datetime.now().strftime("%Y%m%d-%H%M%S")
    manager = Manager(folder / "data")
    engine = manager.models.engines
    voices = manager.config.voices()
    model = next((v.get("model_dir") for v in voices if v.get("family") == "kokoro_tts" and Path(v.get("model_dir") or ".").is_file()), None)
    atomic_json(manager.config.path, {"voices": [v for v in voices if v["id"] == "default"]})
    original = engine._state
    archive = engine.entries["cpu"]["archives"][0]
    part = engine.download_dir / (archive["name"] + ".part")
    def pause_after_chunk(backend, **changes):
        original(backend, **changes)
        if changes.get("received", 0) >= 262144:
            engine.cancel[backend].set()
    report = {}
    try:
        engine._state = pause_after_chunk
        engine.start("cpu")
        engine.threads["cpu"].join(60)
        assert not engine.threads["cpu"].is_alive(), "首个下载块超时"
        assert engine.states["cpu"]["status"] == "paused", engine.states["cpu"]
        report["paused_bytes"] = part.stat().st_size
        assert report["paused_bytes"] == 262144
        print("真实下载已暂停，保留 262144 字节；正在续传。", flush=True)
        engine._state = original
        engine.start("cpu")
        engine.threads["cpu"].join(120)
        assert not engine.threads["cpu"].is_alive(), "引擎下载安装超时"
        assert engine.states["cpu"]["status"] == "done", engine.states["cpu"]
        assert engine.states["cpu"]["resumed_from"] == report["paused_bytes"]
        executable = Path(manager.models.runtime())
        result = subprocess.run([str(executable), "--help"], cwd=executable.parent,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        assert result.returncode == 0, result.stderr or result.stdout
        assert "audiocpp" in (result.stdout + result.stderr).lower()
        report.update(version=engine.catalog["version"], resumed_from=engine.states["cpu"]["resumed_from"],
                      configured_path=str(executable), sha256=archive["sha256"], help_succeeded=True)
        print("官方 CPU 引擎已校验、解压并自动配置，程序启动正常。", flush=True)
        if model:
            manager.models.register(model)
            voice = next(v for v in manager.config.voices() if v.get("family") == "kokoro_tts")
            assert voice["device"] == "cpu" and voice["engine_executable"] == str(executable)
            job_id = manager.submit("新下载引擎的实际推理", voice["id"], [{"text": "你好，这是新下载引擎的语音验证。"}])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                job = manager.store.get(job_id)
                if job["status"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(.2)
            assert job["status"] == "done", job
            report["kokoro"] = validate_wave(manager.root / "audio" / job["segments"][0]["audio"])
            print(f"新引擎实际生成 Kokoro 音频：{report['kokoro']}", flush=True)
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"引擎验证通过：{folder / 'report.json'}", flush=True)
    finally:
        manager.close()


if __name__ == "__main__":
    main()
