from voice_workbench.audio_cpp import build_native_request


def test_model_specific_parameters_are_not_cross_wired():
    base = {"reference_audio": "reference.wav", "speed": 1.5, "emotion": "悲伤", "language": "zh", "request_options": {"seed": 3}}
    index = build_native_request(dict(base, family="index_tts2"), "hello")
    assert index["request"]["options"]["duration_factor"] == 1 / 1.5
    assert index["request"]["options"]["emotion_text"] == "悲伤"
    vox = build_native_request(dict(base, family="voxcpm2", reference_text="original"), "hello")
    assert "emotion_text" not in vox["request"]["options"]
    assert vox["request"]["reference_text"] == "original"
    qwen = build_native_request(dict(base, family="qwen3_tts", reference_text=""), "hello")
    assert qwen["request"]["language"] == "Chinese"
    assert qwen["request"]["options"]["x_vector_only_mode"] is True
