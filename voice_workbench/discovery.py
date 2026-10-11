"""只读取 yovoice 的模型与运行时位置；新任务与配置存入本项目目录。"""
import json
from pathlib import Path
import shutil
import sys

from .schemas import VoiceConfig


def discover_yovoice(data_dir: Path):
    root = Path.home() / ".yovoice"
    state_path = root / "state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    runtime = Path(state.get("runtimePath") or "")
    if not runtime.is_file():
        return []
    references = []
    for voice in state.get("voices", []):
        filename = voice.get("fileName", "")
        if Path(filename).name != filename or not filename:
            continue
        source = root / "voices" / filename
        if source.is_file():
            target = data_dir / "voices" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            references.append(target)
    imported = []
    for model in state.get("models", []):
        identity, path = model.get("id", ""), Path(model.get("path") or "")
        if not path.is_file():
            continue
        family = "index_tts2" if identity.startswith("index-") else "voxcpm2" if identity.startswith("voxcpm2") else "kokoro_tts" if identity.startswith("kokoro-") else None
        if not family:
            continue
        for index, reference in enumerate(([None] if family == "kokoro_tts" else references), 1):
            voice = VoiceConfig(id=f"{identity}-voice{index}", name=f"{identity} · 声音 {index}",
                                backend="audio_cpp", python=sys.executable,
                                engine_executable=str(runtime), model_dir=str(path), family=family,
                                reference_audio=str(reference) if reference else "", device="cuda",
                                revision="imported-1")
            imported.append(voice.model_dump())
    return imported
