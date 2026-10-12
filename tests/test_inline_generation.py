"""Creation-page configuration reaches inference and saved workflow presets."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
from pathlib import Path
import sys
import time
import wave

from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.config import ConfigStore
from voice_workbench.model_info import model_ui
from test_model_capabilities import gguf, kokoro


def make_audio(path):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x10\x00" * 32000)


class Runner:
    def generate(self, request, cancelled):
        make_audio(request["output"])

    def interrupt(self):
        pass


def index_voice(tmp_path):
    path = tmp_path / "index.gguf"
    gguf(path, {"schema_version": 1, "family": "index_tts2", "languages": ["zh", "en"],
                "capabilities": {"clone": ["speaker_reference", "emotion_control"]}})
    exe = tmp_path / "server.exe"
    exe.write_bytes(b"test engine fixture")
    audio = tmp_path / "reference.wav"
    make_audio(audio)
    return {"id": "inline-test", "name": "测试参考声音", "backend": "audio_cpp", "family": "index_tts2",
            "python": sys.executable, "model_dir": str(path), "engine_executable": str(exe),
            "reference_audio": str(audio), "language": "zh", "speed": 1, "emotion": ""}


def test_inline_reference_repairs_missing_preset_without_mutating_it(tmp_path):
    voice = index_voice(tmp_path)
    reference = voice["reference_audio"]
    voice["reference_audio"] = str(tmp_path / "deleted.wav")
    data = tmp_path / "data"
    ConfigStore(data).save_voice(voice)
    with TestClient(create_app(data, runner=Runner())) as client:
        preset = next(v for v in client.get("/voices").json() if v["id"] == voice["id"])
        assert preset["error"]["code"] == "FILE_MISSING"
        response = client.post("/jobs", json={"voice_id": preset["model_name"], "segments": [{"text": "Hello"}],
            "overrides": {"reference_audio": reference, "language": "en", "speed": 1.2, "emotion": "平静"}})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(.01)
        assert job["status"] == "done"
        retained = Path(job["voice"]["reference_audio"])
        assert retained.parent == data / "voices" and retained.read_bytes() == Path(reference).read_bytes()
        assert job["voice"]["language"] == "en"
        assert job["voice"]["speed"] == 1.2 and job["voice"]["emotion"] == "平静"
        assert next(v for v in ConfigStore(data).voices() if v["id"] == voice["id"])["reference_audio"].endswith("deleted.wav")


def test_creation_controls_retain_reference_language_and_auto_detect_new_package(tmp_path, qapp):
    from voice_workbench.model_controls import ModelControls
    voice = index_voice(tmp_path)
    voice.update(model_name="inline-test@version", model_ui=model_ui(voice))
    other = tmp_path / "other.wav"
    make_audio(other)
    controls = ModelControls()
    controls.set_references([voice, {**voice, "reference_audio": str(other), "name": "另一个声音"}])
    controls.bind(voice)
    controls.reference_combo.setCurrentIndex(controls.reference_combo.findData(str(other)))
    controls.language.setCurrentIndex(controls.language.findData("en"))
    controls.bind(dict(voice))
    assert controls.overrides()["reference_audio"] == str(other)
    assert controls.overrides()["language"] == "en"
    replacement = kokoro(tmp_path)
    controls.assets["model_dir"].setText(replacement["model_dir"])
    controls.refresh_capabilities()
    assert controls.info["family"] == "kokoro_tts" and controls.reference.isHidden()
    assert controls.overrides()["family"] == "kokoro_tts"
    assert controls.overrides()["reference_audio"] == "" and controls.overrides()["reference_text"] == ""
    assert controls.native_voice.currentData() in replacement.get("model_ui", model_ui(replacement))["builtin_voices"]
    controls.close()


def test_vox_switch_design_and_continuation_preserves_editable_reference(tmp_path, qapp):
    from voice_workbench.model_controls import ModelControls
    voice = index_voice(tmp_path)
    path = tmp_path / "vox.gguf"
    gguf(path, {"schema_version": 1, "family": "voxcpm2", "capabilities": {"clone": ["speaker_reference"], "design": ["voice_design"]}})
    voice.update(family="voxcpm2", model_dir=str(path))
    controls = ModelControls()
    controls.bind(voice)
    controls.reference_text.setPlainText("你好，这是参考音频的原文。")
    controls.mode.setCurrentIndex(controls.mode.findData("continuation"))
    assert not controls.reference.isHidden() and not controls.reference_text.isHidden()
    controls.mode.setCurrentIndex(controls.mode.findData("design"))
    controls.description.setText("温暖、自然的女声")
    assert controls.overrides()["reference_audio"] == "" and controls.overrides()["reference_text"] == ""
    assert controls.overrides()["voice_description"] == "温暖、自然的女声"
    controls.mode.setCurrentIndex(controls.mode.findData("continuation"))
    assert controls.overrides()["reference_audio"] == voice["reference_audio"]
    assert controls.overrides()["reference_text"] == "你好，这是参考音频的原文。"
    assert controls.overrides()["voice_description"] == ""
    controls.close()


def test_save_from_creation_copies_reference_selects_preset_and_copies_workflow(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog
    from voice_workbench.gui import Window
    from voice_workbench.gui_layout import model_key
    voice = index_voice(tmp_path)
    data = tmp_path / "data"
    ConfigStore(data).save_voice(voice)
    ConfigStore(data).save_voice({**voice, "id": "inline-second", "name": "同一模型另一个预设"})
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("工作流旁白", True))
    with TestClient(create_app(data, runner=Runner())) as client:
        window = Window(8790, data, autostart=False)
        window.timer.stop()
        window.render((client.get("/health").json(), client.get("/voices").json(), [], None))
        assert sum(window.model_combo.itemData(i) == model_key(voice) for i in range(window.model_combo.count())) == 1
        window.refresh_generation_models(voice["id"])
        assert window.voice_combo.count() == 3
        assert window.voice_combo.itemData(0) is None
        window.model_controls.speed.setValue(1.3)
        window.model_controls.language.setCurrentIndex(window.model_controls.language.findData("en"))
        window.model_controls.emotion.setText("平静")
        window.text_edit.setPlainText("Hello")
        assert window.generate_button.isEnabled()
        def call(action, success=None, failure=None):
            value = action(client)
            if success:
                success(value)
        monkeypatch.setattr(window, "call", call)
        window.save_generation_voice()
        saved = next(v for v in ConfigStore(data).voices() if v["name"] == "工作流旁白")
        assert saved["speed"] == 1.3 and saved["language"] == "en" and saved["emotion"] == "平静"
        assert Path(saved["reference_audio"]).parent == data / "voices"
        assert Path(saved["reference_audio"]).read_bytes() == Path(voice["reference_audio"]).read_bytes()
        assert window.current_voice()["id"] == saved["id"] and window.tabs.currentIndex() == 0
        copied = json.loads(qapp.clipboard().text())
        assert copied["model"] == window.current_voice()["model_name"] and len(copied["model_tag"]) == 64
        window.submit([{"text": "Hello"}], "生成页提交测试")
        assert window.job_data["voice"]["speed"] == 1.3 and window.job_data["voice"]["reference_audio"] == saved["reference_audio"]
        window.close()
