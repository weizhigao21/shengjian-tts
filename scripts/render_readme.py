"""Render the current Qt UI with public demo data, without changing user data.

Requires configured local models. Model metadata is read from the installed
profile; inference is never started, and no user audio or text is exported.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication

from voice_workbench import __version__
from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore, atomic_json
from voice_workbench.gui import Window, configure_font, configure_language
from voice_workbench.model_info import model_ui
from voice_workbench.store import Store
from voice_workbench.subtitles import parse_text
from voice_workbench.services.dialogue import entry_revision
from voice_workbench.services.generation import PreparedGeneration

SCRIPT = """[00:00.00]清晨，山谷中的第一束光落在窗边。雾气沿着河面缓缓升起，一座小城从沉睡中醒来。
[00:08.00]At dawn, the first light falls by the window. Mist rises above the river, and a small town wakes from sleep.

[00:18.00]我们沿着街角走向山海。用温暖自然的旁白，讲述旅途中的每个片段；中文和英文，也可以在同一个工作流里完成。
[00:31.00]Every journey begins with a story. Give your words a voice, and let the story travel a little further."""


def demo_wave(path, seconds=5, frequency=180):
    """A synthetic waveform for the screenshot, never a speech-quality sample."""
    rate = 16000
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        samples = []
        for index in range(int(rate * seconds)):
            t = index / rate
            envelope = (.18 + .12 * math.sin(t * 11)) * min(1, t * 8, (seconds - t) * 8)
            value = (math.sin(t * frequency * math.tau) + .25 * math.sin(t * frequency * 2.4 * math.tau)) * envelope
            samples.append(struct.pack("<h", int(value * 16000)))
        stream.writeframes(b"".join(samples))


def prepare(data, original):
    candidates = original.get("model_templates", []) + original.get("voices", [])
    models, identities = [], set()
    for candidate in candidates:
        path = Path(candidate.get("model_dir") or ".")
        if candidate.get("backend") != "audio_cpp" or not path.is_file() or str(path) in identities:
            continue
        identities.add(str(path))
        models.append({**candidate, "id": f"demo-model-{len(models)}", "name": "默认模型设置",
            "reference_audio": "", "reference_text": "", "voice_description": "", "voice_mode": "auto", "emotion": ""})
    vox = next((voice for voice in models if voice.get("family") == "voxcpm2"), None)
    qwen = next((voice for voice in models if voice.get("family") == "qwen3_tts" and model_ui(voice)["variant"] == "custom_voice"), None)
    kokoro = next((voice for voice in models if voice.get("family") == "kokoro_tts"), None)
    if not all((vox, qwen, kokoro)):
        raise RuntimeError("README 截图需要已有的 VoxCPM2、Qwen3 CustomVoice 和 Kokoro 模型。")
    narration = {**vox, "id": "demo-narrator", "name": "温暖旁白", "voice_mode": "design",
        "voice_description": "温暖、清澈的成年女声，语气自然舒缓，适合旅行故事旁白。",
        "request_options": {**vox.get("request_options", {}), "seed": 20261012}}
    voices = [narration,
        {**qwen, "id": "demo-reader", "name": "故事阅读", "voice_mode": "builtin", "native_voice": "serena", "voice_description": "平静、清晰，像讲述一个故事。"},
        {**kokoro, "id": "demo-commentary", "name": "清新解说", "voice_mode": "builtin", "native_voice": "zf_xiaobei"}]
    reference = data / "voices/demo-reference.wav"
    reference.parent.mkdir(parents=True)
    demo_wave(reference)
    atomic_json(data / "config.json", {"voices": voices, "model_templates": models,
        **{key: original[key] for key in ("native_engine", "native_engine_version", "native_engine_backend", "native_engines") if key in original},
        "references": [{"id": "demo-reference", "name": "参考声音 · 女声示例", "reference_audio": str(reference),
                        "reference_text": "欢迎来到山海之间，愿每一段旅程都成为一个好故事。"}]})
    config = ConfigStore(data)
    stamp = datetime(2026, 10, 12, 10, 30).timestamp()
    store = Store(data)
    try:
        for position, (title, voice, script) in enumerate((
                ("山海之间 · 双语旅行旁白", narration, SCRIPT),
                ("午后书信 · 故事阅读", voices[1], "今天的故事，从一封迟来的信开始。\n窗外的雨停了，远处有人轻轻推开了门。"),
                ("产品介绍 · 清新解说", voices[2], "把文字变成声音，让熟悉的音色陪伴每一个创作。"))):
            snapshot, fingerprint = config.resolve(voice["id"])
            job_id = store.create(title, snapshot, fingerprint, parse_text(script), script,
                {"preset_used": True, "preset_id": voice["id"], "preset_name": voice["name"], "preset_modified": False})
            for segment in store.get(job_id)["segments"]:
                store.next(job_id)
                filename = f"demo-{position}-{segment['idx']}.wav"
                seconds = 5 + segment["idx"] * .75
                (data / "audio").mkdir(exist_ok=True)
                demo_wave(data / "audio" / filename, seconds, 140 + position * 80)
                store.finish(job_id, segment["idx"], audio=filename, duration=seconds,
                    generation_seconds=3.2 + segment["idx"] * 1.15)
            with store.lock, store.db:
                store.db.execute("UPDATE jobs SET created=?,updated=? WHERE id=?", (stamp - position * 3600, stamp - position * 3600, job_id))
                store.db.execute("UPDATE segments SET created=? WHERE job_id=?", (stamp - position * 3600, job_id))
            if position == 0:
                main_id = job_id
        entries = [{"id": f"d0{i}a{i}00000000000000000000000000000", "voice_id": voice["id"], "voice_name": voice["name"], "text": text}
            for i, (voice, text) in enumerate(zip(voices, (
                "清晨，山谷中的第一束光落在窗边。我们沿着街角走向山海，旅途从这里开始。",
                "听见了吗？风从山谷吹来，故事也有了新的声音。",
                "那就出发吧，一起去看看下一段风景。")), 1)]
        segments = []
        for entry, voice in zip(entries, voices):
            snapshot, fingerprint = config.resolve(voice["id"])
            segments.append({"text": entry["text"], "source_id": entry["id"], "source_revision": entry_revision(entry, voices),
                "prepared_voice": PreparedGeneration(snapshot, fingerprint, {"preset_used": True, "preset_id": voice["id"],
                    "preset_name": voice["name"], "preset_modified": False, "mode": voice["voice_mode"]})})
        dialogue_id = store.create("山海对话", segments[0]["prepared_voice"].voice, "demo-dialogue", segments,
            "\n".join(e["text"] for e in entries), editor_state={"mode": "dialogue", "article_text": "", "article_mentions": [], "dialogue_entries": entries})
        for segment in store.get(dialogue_id)["segments"]:
            store.next(dialogue_id)
            filename = f"demo-dialogue-{segment['idx']}.wav"
            demo_wave(data / "audio" / filename)
            store.finish(dialogue_id, segment["idx"], audio=filename, duration=5, generation_seconds=2.4 + segment["idx"])
        store.append(dialogue_id, "山海对话", segments[0]["prepared_voice"].voice, "demo-dialogue", segments[:1],
            editor_state=store.get(dialogue_id)["editor_state"])
        store.next(dialogue_id)
        demo_wave(data / "audio/demo-dialogue-4.wav")
        store.finish(dialogue_id, 4, audio="demo-dialogue-4.wav", duration=5, generation_seconds=2.8)
    finally:
        store.close()
    return main_id, narration["id"], dialogue_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "声笺/data/config.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/images")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "voice_workbench/assets/app.png", args.output / "app.png")
    before = args.profile.read_bytes()
    configuration = json.loads(before)
    app = QApplication([])
    app.setStyle("Fusion")
    configure_font(app)
    configure_language(app)
    staging = ROOT / "artifacts/readme-preview"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="demo-", dir=staging) as temporary:
        data = Path(temporary)
        main_id, narration_id, dialogue_job = prepare(data, configuration)
        with TestClient(create_app(data)) as client:
            window = Window(8765, data, autostart=False)
            # Static previews do not need the host's native audio device.
            for player in (window.player, window.reference_player, window.settings.player):
                player.setAudioOutput(None)
            window.timer.stop()
            window.document_timer.stop()
            window.settings.timer.stop()
            window.audio_output.setVolume(0)
            window.reference_output.setVolume(0)
            window.settings.output.setVolume(0)

            def immediate(action, success=None, failure=None):
                try:
                    result = action(client)
                    if success:
                        success(result)
                except Exception as exc:
                    if failure:
                        failure(str(exc))
                    else:
                        raise

            window.call = immediate
            window.selected_job = main_id
            window.ready = True
            window.poll()
            window.open_job_data(client.get("/jobs/" + main_id).json())
            window.refresh_generation_models(narration_id)
            window.resize(1280, 820)
            window.show()
            app.processEvents()
            screenshots = []

            def capture(name):
                # Growing dialogue editors can require another layout pass.
                for _ in range(4):
                    app.processEvents()
                window.clearFocus()
                assert window.grab().save(str(args.output / (name + ".png")))
                screenshots.append(name)

            window.model_controls.advanced_toggle.setChecked(True)
            window.inspector_scroll.verticalScrollBar().setValue(0)
            capture("workbench")
            window.clip_title.click()
            capture("generation-list")
            window.clip_list.hide()
            window.selected_job = dialogue_job
            window.render_jobs(client.get("/jobs").json())
            window.open_job_data(client.get("/jobs/" + dialogue_job).json())
            with closing(Store(data)) as demo_store:
                with demo_store.db:
                    for row in window.dialogue_editor.rows:
                        revision = entry_revision(row.entry(), window.voices, window.generation_voice())
                        demo_store.db.execute("UPDATE segments SET source_revision=? WHERE job_id=? AND source_id=?", (revision, dialogue_job, row.identity))
            window.render_job(client.get("/jobs/" + dialogue_job).json(), force=True)
            capture("dialogue")
            window.clip_title.click()
            capture("dialogue-history")
            window.clip_list.hide()
            window.set_page(1)
            window.library.set_page("presets")
            capture("voice-library")
            window.set_page(2)
            window.update_link_config_for(next(v for v in window.voices if v["id"] == narration_id))
            capture("workflow")
            window.set_page(3)
            window.settings.set_section(0)
            window.settings.model_directory.setText("应用数据目录 / models")
            window.settings.model_search.setText("VoxCPM2")
            window.settings.refresh()
            capture("models")
            window.settings.set_section(4)
            window.settings.refresh()
            capture("engine-settings")
            window.settings.set_section(1)
            window.settings.refresh()
            capture("audio-manager")
            window.player.stop()
            window.player.setSource(QUrl())
            window.reference_player.stop()
            window.reference_player.setSource(QUrl())
            window.settings.stop()
            window.document_dirty = False
            window.close()
            app.processEvents()
    assert args.profile.read_bytes() == before
    report = {"version": __version__, "size": [1280, 820], "images": screenshots,
              "demo_data": True, "inference_started": False, "user_profile_unchanged": True}
    (staging / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
