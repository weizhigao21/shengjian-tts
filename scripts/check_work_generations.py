"""用本机 Kokoro 实测同一作品连续生成，输出仅写入 artifacts。"""
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.manager import Manager
from voice_workbench.runner import validate_wave


def main():
    manager = Manager(ROOT / "artifacts" / "work-generations-check")
    try:
        voice = next(v for v in manager.config.voices() if v["backend"] == "audio_cpp" and v["family"] == "kokoro_tts")
        # Reuse the validation work when this script is rerun; never change user data.
        existing = next((j for j in manager.store.list() if j["title"] == "双语旁白 · 开场"), None)
        work = existing["id"] if existing else manager.store.create("双语旁白 · 开场", {}, "", [], "")
        for text, seed in (("你好，欢迎来到声笺。", 37), ("这些生成记录都保存在同一个作品中。", 38)):
            script = "[00:00.00]" + text
            assert manager.submit("双语旁白 · 开场", voice["id"], [{"text": text, "timestamp": "00:00.00"}],
                overrides={"request_options": {"seed": seed, "text_chunk_size": 64}}, job_id=work, script=script) == work
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                job = manager.store.get(work)
                if job["status"] in ("done", "failed"):
                    break
                time.sleep(.1)
            assert job["status"] == "done", job["segments"][-1]["error"]
            item = job["segments"][-1]
            assert item["voice"]["request_options"]["seed"] == seed
            output = manager.root / "audio" / item["audio"]
            print("generation", item["generation"], "work", work, "audio", output, validate_wave(output), flush=True)
        assert len([j for j in manager.store.list() if j["id"] == work]) == 1
        print("Both generations belong to one persisted work.", flush=True)
    finally:
        manager.close()


if __name__ == "__main__":
    main()
