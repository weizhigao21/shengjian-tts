"""向工作流追加本地服务入口，保留原有模式、选中项和其他 API。"""
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.config import ConfigStore, atomic_json


def main():
    config = ConfigStore(ROOT / "data")
    voice, digest = config.resolve("index-2.5-q8-voice1")
    entry = {"name": "本地配音工作台 · IndexTTS2.5", "url": "http://127.0.0.1:8765",
             "model": f"{voice['id']}@{digest[:12]}", "model_tag": digest, "status": "unknown"}
    atomic_json(ROOT / "integration" / "workflow-api.json", entry)
    path = Path(r"G:\code\双语音声工作流\resources\configs\workflow_config.json")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    apis = value["tts"].setdefault("api_configs", [])
    matching = next((api for api in apis if api.get("name") == entry["name"]), None)
    if matching == entry:
        print("本地配音 API 已存在，未修改配置。")
        return
    backup = ROOT / "integration" / "backups" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup / path.name)
    if matching is not None:
        matching.update(entry)
    else:
        apis.append(entry)
    atomic_json(path, value)
    print("已追加本地配音 API，原有默认选项保留。备份：", backup)


if __name__ == "__main__":
    main()
