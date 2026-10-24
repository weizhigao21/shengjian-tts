"""A reference clip can be recorded from the microphone instead of imported."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import array
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PyQt6.QtCore import QByteArray, QObject, pyqtSignal
from PyQt6.QtMultimedia import QAudioDevice, QAudioFormat
from PyQt6.QtWidgets import QApplication

from voice_workbench.api import create_app
from voice_workbench.desktop import microphone
from voice_workbench.desktop.microphone import MicrophoneRecorder
from voice_workbench.gui import Window
from voice_workbench.library import ReferenceDialog, ReferencePicker
from voice_workbench.runner import validate_wave
from test_service import ControlledRunner


def pcm(seconds, rate=48000, channels=1, value=1000):
    return array.array("h", [value] * (int(rate * seconds) * channels)).tobytes()


def mono_format(rate=48000, channels=1, sample_format=None):
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
    fmt.setChannelCount(channels)
    fmt.setSampleFormat(sample_format or QAudioFormat.SampleFormat.Int16)
    return fmt


class FakeStream(QObject):
    readyRead = pyqtSignal()

    def __init__(self, data=b""):
        super().__init__()
        self.data = data

    def readAll(self):
        data, self.data = self.data, b""
        return QByteArray(data)


class FakeSource(QObject):
    stateChanged = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def takes(tmp_path, monkeypatch):
    """Keep every take inside the test's own directory instead of %TEMP%."""
    root = tmp_path / "takes"
    made = root / (microphone.RECORD_PREFIX + "one")
    made.mkdir(parents=True)
    monkeypatch.setattr(microphone, "recording_folder", lambda: made)
    return root


def recording(recorder, raw, fmt=None):
    """Put the recorder where it stands half way through a take."""
    recorder._format = fmt or mono_format()
    recorder._raw = bytearray(raw)
    recorder._source = FakeSource()
    recorder._stream = FakeStream()
    recorder.recording = True
    recorder._began = time.monotonic()
    return recorder


class FakeDevice:
    def __init__(self, supported=(), preferred=None):
        self.supported, self.preferred = set(supported), preferred or mono_format()

    def isNull(self):
        return False

    def isFormatSupported(self, fmt):
        return (fmt.sampleRate(), fmt.channelCount()) in self.supported

    def preferredFormat(self):
        return self.preferred


def test_a_stereo_device_is_averaged_down_to_one_channel():
    stereo = array.array("h", [100, 300, -100, -300, 0, 0]).tobytes()
    assert array.array("h", microphone.to_mono(stereo, 2)) == array.array("h", [200, -200, 0])
    assert microphone.to_mono(stereo, 1) == stereo
    # A partial frame at the end is dropped instead of shifting every later sample.
    assert microphone.to_mono(stereo + b"\x01", 2) == microphone.to_mono(stereo, 2)


def test_float_and_wide_samples_become_sixteen_bit_without_wrapping_around():
    loud = array.array("f", [0.0, 0.5, 1.0, -1.0, 1.5, -1.5]).tobytes()
    assert array.array("h", microphone.to_int16(loud, QAudioFormat.SampleFormat.Float)) == \
        array.array("h", [0, 16384, 32767, -32767, 32767, -32768])
    wide = array.array("i", [0, 2147483647, -2147483648]).tobytes()
    assert array.array("h", microphone.to_int16(wide, QAudioFormat.SampleFormat.Int32)) == \
        array.array("h", [0, 32767, -32768])
    with pytest.raises(ValueError):
        microphone.to_int16(b"\x00\x00", "UInt8")


def test_a_take_is_written_as_the_mono_wav_the_reference_check_accepts(tmp_path):
    target = tmp_path / "take.wav"
    assert abs(microphone.write_wav(target, pcm(1.5), 48000) - 1.5) < 0.001
    with wave.open(str(target), "rb") as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (1, 2, 48000)
    assert abs(validate_wave(target)["duration"] - 1.5) < 0.001


def test_the_supported_plain_format_wins_over_the_device_preference():
    device = FakeDevice(supported={(44100, 2)}, preferred=mono_format(sample_format=QAudioFormat.SampleFormat.Float))
    chosen = microphone.pick_format(device)
    assert (chosen.sampleRate(), chosen.channelCount()) == (44100, 2)
    assert chosen.sampleFormat() == QAudioFormat.SampleFormat.Int16
    # A device that refuses every plain format still records through its own preference.
    fallback = microphone.pick_format(FakeDevice(preferred=mono_format(sample_format=QAudioFormat.SampleFormat.Float)))
    assert fallback.sampleFormat() == QAudioFormat.SampleFormat.Float
    assert microphone.pick_format(FakeDevice(preferred=mono_format(sample_format=QAudioFormat.SampleFormat.Unknown))) is None


def test_a_missing_microphone_is_explained_and_records_nothing(qapp, takes):
    recorder = MicrophoneRecorder(device=QAudioDevice())
    errors = []
    recorder.failed.connect(errors.append)
    recorder.start()
    assert errors and "麦克风" in errors[0]
    assert not recorder.recording and not list(takes.rglob("*.wav"))


def test_stopping_a_take_hands_back_a_wav_and_gives_the_device_back(qapp, takes):
    recorder = MicrophoneRecorder()
    source = recording(recorder, pcm(1.5))._source
    # Data still waiting in the device buffer at stop time belongs to the take too.
    recorder._stream.data = pcm(0.5)
    finished, errors = [], []
    recorder.finished.connect(finished.append)
    recorder.failed.connect(errors.append)
    recorder.stop()
    assert not errors and len(finished) == 1
    path = Path(finished[0])
    assert path.parent.parent == takes and path.suffix == ".wav"
    assert abs(validate_wave(path)["duration"] - 2.0) < 0.01
    assert source.stopped and not recorder.recording and not recorder.timer.isActive()


def test_a_take_under_one_second_is_refused_instead_of_saved(qapp, takes):
    recorder = MicrophoneRecorder()
    recording(recorder, pcm(0.4))
    finished, errors = [], []
    recorder.finished.connect(finished.append)
    recorder.failed.connect(errors.append)
    recorder.stop()
    assert not finished and errors and "1 秒" in errors[0]
    assert not list(takes.rglob("*.wav"))


def test_a_float_device_is_measured_by_its_real_length_not_half_of_it(qapp, takes):
    """Four bytes per sample: a 0.6 s take must not be mistaken for 1.2 s."""
    recorder = MicrophoneRecorder()
    quiet = array.array("f", [0.1] * int(48000 * 0.6)).tobytes()
    recording(recorder, quiet, mono_format(sample_format=QAudioFormat.SampleFormat.Float))
    finished, errors = [], []
    recorder.finished.connect(finished.append)
    recorder.failed.connect(errors.append)
    recorder.stop()
    assert not finished and errors and "0.6 秒" in errors[0]


def test_the_sixty_second_ceiling_stops_a_take_on_its_own(qapp, takes):
    recorder = MicrophoneRecorder()
    recording(recorder, pcm(1.0))
    recorder.elapsed = lambda: microphone.MAX_SECONDS
    finished = []
    recorder.finished.connect(finished.append)
    recorder.tick()
    assert not recorder.recording and recorder._source is None
    assert len(finished) == 1


def prepare(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    client = TestClient(create_app(tmp_path, runner=ControlledRunner()))
    client.__enter__()
    window = Window(8795, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()

    def immediate(action, success=None, failure=None):
        try:
            value = action(client)
            if success:
                success(value)
        except Exception as exc:
            if failure:
                failure(str(exc))
            else:
                raise
    window.call = immediate
    window.ready = True
    window.poll()
    QApplication.processEvents()
    return client, window, window.open_dialog(ReferencePicker(window, window.model_controls))


def test_the_picker_saves_a_take_and_drops_the_temporary_copy(tmp_path, qtbot, monkeypatch, takes):
    client, window, picker = prepare(tmp_path, qtbot, monkeypatch)
    try:
        take = takes / (microphone.RECORD_PREFIX + "one") / "录音-test.wav"
        microphone.write_wav(take, pcm(1.5), 48000)
        monkeypatch.setattr(picker.recorder, "start", lambda: picker.recorder.finished.emit(str(take)))
        picker.record_button.click()
        dialog = next(d for d in window.dialogs if isinstance(d, ReferenceDialog))
        assert Path(dialog.path) == take
        assert "麦克风录音" in dialog.file.text() and "1.5 秒" in dialog.file.text()
        assert dialog.name.text().startswith("录音 ") and dialog.text.toPlainText() == ""
        dialog.name.setText("刚录的声音")
        dialog.confirm.click()
        reference = next(r for r in client.get("/references").json() if r["name"] == "刚录的声音")
        retained = Path(reference["reference_audio"])
        assert retained.parent == tmp_path / "voices" and retained != take
        assert abs(validate_wave(retained)["duration"] - 1.5) < 0.001
        assert not take.exists() and not take.parent.exists()
        assert picker.recording_path == ""
    finally:
        window.close()
        client.__exit__(None, None, None)


def test_a_second_click_stops_a_live_take_instead_of_starting_another(tmp_path, qtbot, monkeypatch):
    client, window, picker = prepare(tmp_path, qtbot, monkeypatch)
    try:
        started, stopped = [], []
        monkeypatch.setattr(picker.recorder, "start", lambda: started.append(1))
        monkeypatch.setattr(picker.recorder, "stop", lambda: stopped.append(1))
        picker.start_recording()
        assert started == [1] and picker.import_button.isEnabled() is False
        assert picker.record_button.property("kind") == "recording"
        picker.show_elapsed(4.2)
        assert picker.record_button.text() == "停止 · 4.2 秒"
        picker.recorder.recording = True
        picker.toggle_recording()
        assert stopped == [1] and started == [1]
    finally:
        window.close()
        client.__exit__(None, None, None)


def test_a_microphone_that_will_not_open_leaves_the_picker_usable(tmp_path, qtbot, monkeypatch):
    client, window, picker = prepare(tmp_path, qtbot, monkeypatch)
    try:
        def refuse():
            picker.recorder.failed.emit("麦克风打不开，可能被其他程序占用；关掉占用麦克风的程序再试。")
        monkeypatch.setattr(picker.recorder, "start", refuse)
        picker.record_button.click()
        assert not picker.error.isHidden() and "麦克风" in picker.error.text()
        assert picker.record_button.text() == "录音"
        assert picker.record_button.property("kind") == "small"
        assert picker.import_button.isEnabled() and picker.preview_button.isEnabled()
        assert not picker.confirm.isEnabled()
        # Nothing was recorded, so no naming dialog may appear.
        assert not [d for d in window.dialogs if isinstance(d, ReferenceDialog)]
    finally:
        window.close()
        client.__exit__(None, None, None)
