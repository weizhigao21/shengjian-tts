"""Record a reference clip straight from the default microphone.

A reference has to be a real WAV of 1–60 seconds, so the recorder writes one
16-bit mono file per take. Qt hands back whatever format the device prefers, and
on Windows that is frequently 32-bit float; everything is converted to the single
format the rest of the app validates and the engines read.
"""
from __future__ import annotations
import array
import tempfile
import time
import wave
from pathlib import Path
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSource, QMediaDevices


MIN_SECONDS = 1.0
MAX_SECONDS = 60.0
TICK_MS = 100
RECORD_PREFIX = "shengjian-mic-"
# Mono first: the reference is one voice, and stereo would only double the file.
CANDIDATES = ((48000, 1), (44100, 1), (48000, 2), (44100, 2))


def sample_format_name(sample_format):
    return getattr(sample_format, "name", str(sample_format))


def to_int16(raw, sample_format):
    """Convert captured samples to signed 16-bit without changing the sample rate."""
    kind = sample_format_name(sample_format)
    if kind == "Int16":
        return bytes(raw)
    if kind == "Float":
        samples = array.array("f")
        samples.frombytes(bytes(raw)[: len(raw) // 4 * 4])
        return array.array("h", (max(-32768, min(32767, int(round(v * 32767)))) for v in samples)).tobytes()
    if kind == "Int32":
        samples = array.array("i")
        samples.frombytes(bytes(raw)[: len(raw) // 4 * 4])
        return array.array("h", (v >> 16 for v in samples)).tobytes()
    raise ValueError(f"不支持的录音采样格式：{kind}")


def to_mono(raw, channels):
    """Average the channels: a stereo device is common even when mono was asked for."""
    if channels <= 1:
        return bytes(raw)
    frame = 2 * channels
    usable = len(raw) // frame * frame
    samples = array.array("h")
    samples.frombytes(bytes(raw[:usable]))
    mono = array.array("h", (sum(samples[i:i + channels]) // channels for i in range(0, len(samples), channels)))
    return mono.tobytes()


def write_wav(path, raw, sample_rate):
    """Write one canonical reference WAV and return its duration in seconds."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(raw)
    return len(raw) / 2 / sample_rate if sample_rate else 0.0


def frame_seconds(raw, sample_rate, frame_bytes):
    """Length of a take in seconds, from the device's own frame size.

    A float device stores four bytes per sample, so assuming two would report a
    take twice as long as it is and let a half-second clip pass the 1 s floor.
    """
    return len(raw) // max(1, frame_bytes) / sample_rate if sample_rate else 0.0


def pick_format(device):
    """Prefer a plain 16-bit mono format; fall back to the device's own preference."""
    for rate, channels in CANDIDATES:
        wanted = QAudioFormat()
        wanted.setSampleRate(rate)
        wanted.setChannelCount(channels)
        wanted.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if device.isFormatSupported(wanted):
            return wanted
    preferred = device.preferredFormat()
    if (preferred.sampleRate() > 0 and preferred.channelCount() in (1, 2)
            and sample_format_name(preferred.sampleFormat()) in ("Int16", "Float", "Int32")):
        return preferred
    return None


def recording_folder():
    """A throwaway folder per take; the clip is copied into the data directory on save."""
    return Path(tempfile.mkdtemp(prefix=RECORD_PREFIX))


class MicrophoneRecorder(QObject):
    """One microphone take: start, stop, and hand back a WAV path."""

    started = pyqtSignal()
    ticked = pyqtSignal(float)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, parent=None, device=None):
        super().__init__(parent)
        self._device = device
        self._source = None
        self._stream = None
        self._format = None
        self._raw = bytearray()
        self._began = 0.0
        self.recording = False
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)

    def device(self):
        return self._device if self._device is not None else QMediaDevices.defaultAudioInput()

    def elapsed(self):
        return time.monotonic() - self._began if self.recording else 0.0

    def start(self):
        if self.recording:
            return
        device = self.device()
        if device is None or device.isNull():
            self.failed.emit("没有找到可用的麦克风。插好麦克风后重试，或在系统设置里允许本程序使用麦克风。")
            return
        wanted = pick_format(device)
        if wanted is None:
            self.failed.emit("这个麦克风不支持 16 位录音，换一个输入设备再试。")
            return
        source = QAudioSource(device, wanted, self)
        source.setBufferSize(max(wanted.bytesForDuration(200_000), 4096))
        stream = source.start()
        if stream is None or source.error() != QAudio.Error.NoError:
            source.deleteLater()
            self.failed.emit("麦克风打不开，可能被其他程序占用；关掉占用麦克风的程序再试。")
            return
        self._source, self._stream, self._format = source, stream, wanted
        self._raw = bytearray()
        self._began = time.monotonic()
        self.recording = True
        stream.readyRead.connect(self.collect)
        source.stateChanged.connect(self.device_state)
        self.timer.start()
        self.started.emit()

    def collect(self):
        if self._stream is None:
            return
        chunk = self._stream.readAll()
        if chunk:
            self._raw += bytes(chunk)
            # A device that keeps the buffer full must not push past the ceiling.
            if self.elapsed() >= MAX_SECONDS:
                self.stop()

    def device_state(self, state):
        # The device going away mid-take still keeps whatever was captured.
        if self.recording and state == QAudio.State.StoppedState:
            self.stop()

    def tick(self):
        seconds = self.elapsed()
        if seconds >= MAX_SECONDS:
            self.stop()
            return
        self.ticked.emit(seconds)

    def release(self):
        """Give the device back without deciding what the take was worth."""
        self.recording = False
        self.timer.stop()
        source, self._source = self._source, None
        stream, self._stream = self._stream, None
        if source is not None:
            try:
                source.stateChanged.disconnect(self.device_state)
            except TypeError:
                pass
            source.stop()
            source.deleteLater()
        if stream is not None:
            try:
                stream.readyRead.disconnect(self.collect)
            except TypeError:
                pass

    def cancel(self):
        self.release()
        self._raw = bytearray()

    def stop(self):
        if not self.recording:
            return
        self.collect()
        raw, wanted = bytes(self._raw), self._format
        rate, channels = wanted.sampleRate(), wanted.channelCount()
        self.release()
        self._raw = bytearray()
        seconds = frame_seconds(raw, rate, wanted.bytesPerFrame())
        if seconds < MIN_SECONDS:
            self.failed.emit(f"只录到 {seconds:.1f} 秒，参考声音至少需要 1 秒。按住按钮说完再点停止。")
            return
        try:
            mono = to_mono(to_int16(raw, wanted.sampleFormat()), channels)
            target = recording_folder() / f"录音-{time.strftime('%Y%m%d-%H%M%S')}.wav"
            write_wav(target, mono, rate)
        except (OSError, ValueError) as exc:
            self.failed.emit(f"录音没能写成 WAV：{exc}")
            return
        self.finished.emit(str(target))
