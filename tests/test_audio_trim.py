"""The lead-in cut: it must shorten a pause and never damage real audio."""
import array
import math
import wave
from pathlib import Path

import pytest

from voice_workbench.audio_trim import trim_leading_silence


def write_wave(path, seconds, rate=48000, channels=1, lead=0.0, amplitude=8000, width=2):
    """A tone (optionally preceded by digital silence) at a given sample width."""
    total = round(seconds * rate)
    head = round(lead * rate)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        samples = array.array("h")
        for i in range(total):
            value = 0 if i < head else round(amplitude * math.sin(2 * math.pi * 220 * i / rate))
            for _ in range(channels):
                samples.append(value)
        if width == 2:
            stream.writeframes(samples.tobytes())
        else:
            stream.writeframes(samples.tobytes()[: len(samples) * width])
    return path


def read_wave(path):
    with wave.open(str(path), "rb") as stream:
        rate, channels = stream.getframerate(), stream.getnchannels()
        width, frames = stream.getsampwidth(), stream.getnframes()
        raw = stream.readframes(frames)
    values = array.array("h", raw) if width == 2 else array.array("B", raw)
    return {"rate": rate, "channels": channels, "width": width, "frames": frames, "values": values}


def test_cuts_a_long_pause_but_keeps_the_voice(tmp_path):
    path = write_wave(tmp_path / "clip.wav", 0.8, lead=0.35)
    head = 0.35

    result = trim_leading_silence(path)

    # 30 ms of padding stays in front of the onset.
    assert result["trimmed"] == pytest.approx(head - 0.03, abs=0.003)
    audio = read_wave(path)
    assert audio["frames"] / audio["rate"] == pytest.approx(0.8 - head + 0.03, abs=0.005)
    # The fade-in starts at zero so the clip cannot click.
    assert abs(audio["values"][0]) < 200


def test_a_take_that_already_starts_on_the_voice_is_left_alone(tmp_path):
    path = write_wave(tmp_path / "tight.wav", 0.6, lead=0.0)
    before = path.read_bytes()

    assert trim_leading_silence(path) == {}
    assert path.read_bytes() == before


def test_a_pause_shorter_than_the_minimum_is_not_rewritten(tmp_path):
    path = write_wave(tmp_path / "short.wav", 0.6, lead=0.05)
    before = path.read_bytes()

    assert trim_leading_silence(path) == {}
    assert path.read_bytes() == before


def test_an_empty_take_is_not_trimmed(tmp_path):
    path = write_wave(tmp_path / "empty.wav", 0.4, lead=0.0, amplitude=6)
    before = path.read_bytes()

    assert trim_leading_silence(path) == {}
    assert path.read_bytes() == before


def test_stereo_keeps_both_channels_in_step(tmp_path):
    path = write_wave(tmp_path / "stereo.wav", 0.7, lead=0.3, channels=2)
    result = trim_leading_silence(path)

    audio = read_wave(path)
    assert audio["channels"] == 2
    assert result["trimmed"] == pytest.approx(0.27, abs=0.003)
    # Frames, not samples: every sample slot is whole, so channels stay aligned.
    assert audio["frames"] * 2 == len(audio["values"])
    assert audio["frames"] / audio["rate"] == pytest.approx(0.4 + 0.03, abs=0.005)


def test_a_format_it_cannot_prove_safe_is_skipped(tmp_path):
    # Claim 24-bit; the module must refuse rather than re-encode.
    path = tmp_path / "other.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 3, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00\x00" * 48000)
    before = path.read_bytes()
    assert trim_leading_silence(path) == {}
    assert path.read_bytes() == before


def test_a_missing_file_is_not_an_exception(tmp_path):
    assert trim_leading_silence(tmp_path / "nope.wav") == {}


def test_a_long_digital_pause_is_removed_past_the_short_ceiling(tmp_path):
    # qwen3_tts answers with 0.6 s of exact zeros as a rule (median over 300
    # production takes) and occasionally several seconds. The short ceiling
    # exists for noisy heads and must not apply to true digital silence.
    path = write_wave(tmp_path / "long.wav", 2.0, rate=24000, lead=1.5)

    result = trim_leading_silence(path)

    assert result["trimmed"] == pytest.approx(1.47, abs=0.02)
    audio = read_wave(path)
    assert audio["frames"] / audio["rate"] == pytest.approx(0.5 + 0.03, abs=0.02)


def test_a_noisy_head_keeps_the_short_ceiling(tmp_path):
    # A -44 dBFS floor instead of zeros: a large cut here could mean the onset
    # search ran past a quiet real opening, so the pause is left in place.
    path = tmp_path / "noisy.wav"
    rate, seconds, head, noise = 48000, 1.6, 0.9, 300
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        samples = array.array("h")
        for i in range(round(seconds * rate)):
            if i < round(head * rate):
                samples.append(noise if i % 2 else -noise)
            else:
                samples.append(round(8000 * math.sin(2 * math.pi * 220 * i / rate)))
        stream.writeframes(samples.tobytes())
    before = path.read_bytes()

    assert trim_leading_silence(path) == {}
    assert path.read_bytes() == before


def test_the_cut_can_be_switched_off_in_config(tmp_path):
    from voice_workbench.config import ConfigStore, atomic_json

    store = ConfigStore(tmp_path)
    assert store.trim_silence() is True
    atomic_json(store.path, {**store.read(), "trim_silence": False})
    assert store.trim_silence() is False


class LeadRunner:
    """Stands in for the engine: every take starts with 0.3 s of silence."""

    def generate(self, request, cancelled):
        write_wave(Path(request["output"]), 1.0, lead=0.3)

    def interrupt(self):
        pass


def test_a_generated_clip_reaches_the_library_without_the_pause(tmp_path):
    from fastapi.testclient import TestClient
    from voice_workbench.api import create_app
    from voice_workbench.config import ConfigStore
    from test_inline_generation import index_voice
    from test_library_and_deletion import wait_job

    data = tmp_path / "data"
    voice = index_voice(tmp_path)
    ConfigStore(data).save_voice(voice)
    with TestClient(create_app(data, runner=LeadRunner())) as client:
        preset = next(v for v in client.get("/voices").json() if v["id"] == voice["id"])
        response = client.post("/jobs", json={"voice_id": preset["model_name"],
                                              "segments": [{"text": "测试"}]})
        assert response.status_code == 202
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "done", job
        audio = data / "audio" / job["segments"][0]["audio"]

    trimmed = read_wave(audio)
    # 30 ms of padding, not the whole 0.3 s pause.
    assert trimmed["frames"] / trimmed["rate"] == pytest.approx(0.73, abs=0.01)
    assert job["segments"][0]["duration"] == pytest.approx(0.73, abs=0.01)
