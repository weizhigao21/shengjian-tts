import json
from pathlib import Path
import struct
import sys
import time
import wave

import pytest

from voice_workbench.audio_cpp import build_native_request
from voice_workbench.errors import VoiceError
from voice_workbench.model_info import model_ui, validate_capabilities


def string(value):
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def gguf(path, spec, voices=None, config=None):
    """Small valid metadata fixture with embedded files, no model weights."""
    blobs = {"config.json": json.dumps(config or {}).encode()}
    if voices is not None:
        blobs["voices.json"] = json.dumps({v: {} for v in voices}).encode()
    names = list(blobs)
    offsets = [0]
    for raw in blobs.values():
        offsets.append(offsets[-1] + len(raw))
    def array(type_, items):
        return struct.pack("<IQ", type_, len(items)) + b"".join(string(v) if type_ == 8 else struct.pack("<Q", v) for v in items)
    fields = {
        "audiocpp.model_spec.family": (8, string(spec["family"])),
        "audiocpp.model_spec.json": (8, string(json.dumps(spec))),
        "audiocpp.embedded_files.names": (9, array(8, names)),
        "audiocpp.embedded_files.offsets": (9, array(10, offsets)),
        "audiocpp.embedded_files.data": (9, struct.pack("<IQ", 0, offsets[-1]) + b"".join(blobs.values())),
    }
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(fields)) + b"".join(string(key) + struct.pack("<I", type_) + raw for key, (type_, raw) in fields.items()))


def kokoro(tmp_path, speed=True):
    path = tmp_path / "voice.gguf"
    gguf(path, {"schema_version": 1, "family": "kokoro_tts", "capabilities": {"tts": ["built_in_voices"]},
                "ui": {"builtin_voices": ["af_heart", "zf_xiaobei", "jf_alpha"]},
                "options": {"request": [{"name": "speed", "type": "float"}] if speed else []}},
         voices=["af_heart", "zf_xiaobei"])
    return {"backend": "audio_cpp", "family": "kokoro_tts", "model_dir": str(path), "language": "zh", "native_voice": "zf_xiaobei", "speed": 1, "emotion": ""}


def test_package_voices_override_broader_family_list(tmp_path):
    voice = kokoro(tmp_path)
    info = model_ui(voice)
    assert info["modes"] == ["builtin"]
    assert info["source"] == "gguf" and info["builtin_voices"] == ["af_heart", "zf_xiaobei"]
    assert info["languages"] == ["en-us", "zh"]
    assert info["emotion"] is False and info["reference_text"] is False
    for mode in ("clone", "design"):
        with pytest.raises(VoiceError, match="不支持"):
            validate_capabilities({**voice, "voice_mode": mode}, info)
    with pytest.raises(VoiceError, match="没有内置音色"):
        validate_capabilities({**voice, "native_voice": "jf_alpha"}, info)
    with pytest.raises(VoiceError, match="不支持声音描述"):
        validate_capabilities({**voice, "voice_description": "男声"}, info)


def test_kokoro_request_never_clones_and_language_tracks_voice(tmp_path):
    voice = {**kokoro(tmp_path), "native_voice": "af_heart", "reference_audio": "stale.wav",
             "request_options": {"instruct": "stale design", "emotion_text": "stale emotion", "seed": 3}}
    request = build_native_request(voice, "Hello")["request"]
    assert "voice_ref" not in request
    assert request["voice_id"] == "af_heart" and request["options"]["language"] == "en-us"
    assert request["options"] == {"seed": 3, "language": "en-us", "speed": 1}


def test_old_package_without_speed_hides_and_rejects_nondefault_speed(tmp_path):
    voice = kokoro(tmp_path, speed=False)
    assert model_ui(voice)["speed"] is False
    with pytest.raises(VoiceError, match="未提供语速控制"):
        validate_capabilities({**voice, "speed": 1.2})


def test_voxcpm_design_is_driven_by_runtime_config_and_requires_description(tmp_path):
    path = tmp_path / "vox.gguf"
    gguf(path, {"family": "voxcpm2"})
    specs = tmp_path / "model_specs"
    specs.mkdir()
    (specs / "voxcpm2.json").write_text(json.dumps({"schema_version": 1, "family": "voxcpm2", "capabilities": {"clone": ["speaker_reference"], "design": ["voice_design"]}, "languages": ["zh", "en"]}))
    voice = {"backend": "audio_cpp", "family": "voxcpm2", "model_dir": str(path), "engine_executable": str(tmp_path / "server.exe"),
             "language": "zh", "speed": 1, "emotion": "", "voice_mode": "design"}
    assert "design" in model_ui(voice)["modes"]
    with pytest.raises(VoiceError, match="需要声音描述"):
        validate_capabilities(voice)
    voice.update(voice_description="低沉男声", reference_audio="should-not-be-used.wav")
    assert validate_capabilities(voice) == "design"
    request = build_native_request(voice, "你好")["request"]
    assert request["text"] == "(低沉男声)你好" and "voice_ref" not in request


def test_qwen_variants_do_not_offer_other_variants_modes(tmp_path):
    path = tmp_path / "qwen.gguf"
    gguf(path, {"schema_version": 1, "family": "qwen3_tts", "capabilities": {"clone": ["speaker_reference"], "design": ["voice_design"]}}, config={"tts_model_type": "base"})
    voice = {"backend": "audio_cpp", "family": "qwen3_tts", "model_dir": str(path)}
    assert model_ui(voice)["modes"] == ["clone"]
    gguf(path, {"schema_version": 1, "family": "qwen3_tts"}, config={"tts_model_type": "voice_design"})
    assert model_ui(voice)["modes"] == ["design"]


def test_wrong_family_is_reported_before_inference(tmp_path):
    voice = kokoro(tmp_path)
    with pytest.raises(VoiceError, match="属于 kokoro_tts"):
        validate_capabilities({**voice, "family": "index_tts2"})


def test_api_selected_timbre_is_saved_to_job_not_preset(tmp_path):
    from fastapi.testclient import TestClient
    from voice_workbench.api import create_app
    from voice_workbench.config import ConfigStore
    class Runner:
        def generate(self, request, cancelled):
            with wave.open(request["output"], "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x10\x00" * 16000)
        def interrupt(self):
            pass
    voice = kokoro(tmp_path)
    executable = tmp_path / "server.exe"
    executable.write_bytes(b"test fixture")
    voice.update(id="kokoro-test", name="Kokoro test", python=sys.executable, engine_executable=str(executable))
    data = tmp_path / "data"
    ConfigStore(data).save_voice(voice)
    with TestClient(create_app(data, runner=Runner())) as client:
        item = next(v for v in client.get("/voices").json() if v["id"] == voice["id"])
        assert item["model_ui"]["modes"] == ["builtin"]
        base = {"voice_id": item["model_name"], "segments": [{"text": "Hello"}]}
        response = client.post("/jobs", json={**base, "overrides": {"native_voice": "af_heart", "voice_mode": "builtin"}})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] == "done":
                break
            time.sleep(.01)
        assert job["status"] == "done" and job["voice"]["native_voice"] == "af_heart"
        persisted = next(v for v in client.get("/voices").json() if v["id"] == voice["id"])
        assert persisted["native_voice"] == "zf_xiaobei" and persisted["model_name"] == item["model_name"]
        for overrides in ({"native_voice": "fake"}, {"voice_mode": "clone"}, {"voice_mode": "design", "voice_description": "男声"}):
            assert client.post("/jobs", json={**base, "overrides": overrides}).status_code == 400


def test_model_controls_filter_voices_and_keep_selection_during_refresh(tmp_path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from voice_workbench.model_controls import ModelControls
    app = QApplication.instance() or QApplication([])
    voice = kokoro(tmp_path)
    voice.update(id="kokoro", model_name="kokoro@version")
    controls = ModelControls()
    controls.bind(voice)
    assert controls.mode.count() == 1 and controls.mode.currentData() == "builtin"
    assert controls.emotion.isHidden() and controls.description.isHidden()
    controls.language.setCurrentIndex(controls.language.findData("en-us"))
    assert controls.native_voice.count() == 1 and controls.native_voice.currentData() == "af_heart"
    controls.bind(dict(voice))
    assert controls.overrides()["native_voice"] == "af_heart", "Polling reset the current timbre"
    controls.bind({"id": "index", "model_name": "index@version", "backend": "index_tts", "index_version": "2.5"})
    assert controls.mode.currentData() == "clone" and controls.native_voice.isHidden()
    assert not controls.emotion.isHidden() and "native_voice" not in controls.overrides()
    controls.close()
