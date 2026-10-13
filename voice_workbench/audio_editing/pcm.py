"""Bounded PCM WAV edits; no model, Qt or optional media executable required."""
import array
import math
import sys
import wave
from pathlib import Path
from ..errors import VoiceError

MAX_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 1800


def inspect_wave(path):
    try:
        path = Path(path)
        if not path.is_file() or not 44 <= path.stat().st_size <= MAX_BYTES:
            raise ValueError("文件不存在、为空或超过 512 MB")
        with wave.open(str(path), "rb") as stream:
            rate, channels, width, frames = (stream.getframerate(), stream.getnchannels(),
                                           stream.getsampwidth(), stream.getnframes())
            if channels not in (1, 2) or width not in (1, 2, 3, 4) or not 8000 <= rate <= 192000:
                raise ValueError("需要 8–192 kHz、单声道或双声道的 PCM WAV")
            if frames < 1 or frames / rate > MAX_SECONDS:
                raise ValueError("音频需要有声音，且长度不超过 30 分钟")
            if stream.getcomptype() != "NONE":
                raise ValueError("需要未压缩的 PCM WAV")
            # Validate the payload in bounded blocks, including truncated WAVs.
            remaining = frames
            while remaining:
                count = min(remaining, 65536)
                if len(stream.readframes(count)) != count * channels * width:
                    raise ValueError("WAV 数据不完整")
                remaining -= count
        return {"duration": frames / rate, "sample_rate": rate, "channels": channels,
                "sample_width": width, "frames": frames, "size": path.stat().st_size}
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise VoiceError("EDIT_AUDIO_INVALID", f"无法导入音频：{exc}",
                         "第一版支持 PCM WAV；请先将 MP3、浮点 WAV 等转为 PCM WAV。", "音频编辑") from exc


def frame_range(info, start, end):
    end = info["duration"] if end is None else end
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= info["duration"] + 0.000001:
        raise VoiceError("EDIT_SELECTION_INVALID", "选区需在音频范围内，结束时间必须大于开始时间", stage="音频编辑")
    first = round(start * info["sample_rate"])
    last = min(info["frames"], round(end * info["sample_rate"]))
    if last <= first:
        raise VoiceError("EDIT_SELECTION_INVALID", "选区太短，没有完整音频采样", stage="音频编辑")
    return first, last


def _samples(raw, width):
    if width == 1:
        return [v - 128 for v in raw]
    if width in (2, 4):
        values = array.array("h" if width == 2 else "i", raw)
        if sys.byteorder != "little":
            values.byteswap()
        return values
    return [int.from_bytes(raw[i:i+3], "little", signed=True) for i in range(0, len(raw), 3)]


def _encode(values, width):
    if width == 1:
        return bytes(v + 128 for v in values)
    if width in (2, 4):
        result = array.array("h" if width == 2 else "i", values)
        if sys.byteorder != "little":
            result.byteswap()
        return result.tobytes()
    return b"".join(v.to_bytes(3, "little", signed=True) for v in values)


def render_wave(source, output, *, start=0, end=None, gain_db=0, fade_in=0, fade_out=0,
                append=None, cancelled=None):
    info = inspect_wave(source)
    first, last = frame_range(info, start, end)
    count = last - first
    rate, channels, width = info["sample_rate"], info["channels"], info["sample_width"]
    if fade_in + fade_out > count / rate:
        raise VoiceError("EDIT_FADE_INVALID", "淡入和淡出时长合计不能超过选区长度", stage="音频编辑")
    if append:
        other = inspect_wave(append)
        if any(info[k] != other[k] for k in ("sample_rate", "channels", "sample_width")):
            raise VoiceError("EDIT_FORMAT_MISMATCH", "拼接音频的采样率、声道和位深需与当前音频相同", stage="音频编辑")
        if count / rate + other["duration"] > MAX_SECONDS or (count + other["frames"]) * channels * width > MAX_BYTES - 44:
            raise VoiceError("EDIT_AUDIO_TOO_LARGE", "拼接结果超过 30 分钟或 512 MB", stage="音频编辑")
    gain = 10 ** (gain_db / 20)
    minimum, maximum = -(2 ** (width * 8 - 1)), 2 ** (width * 8 - 1) - 1
    transform = gain_db != 0 or fade_in != 0 or fade_out != 0
    clipped = 0
    with wave.open(str(source), "rb") as stream, wave.open(str(output), "wb") as dest:
        dest.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        stream.setpos(first)
        done = 0
        while done < count:
            if cancelled and cancelled.is_set():
                raise VoiceError("CANCELLED", "编辑已取消")
            frames = min(16384, count - done)
            raw = stream.readframes(frames)
            if len(raw) != frames * channels * width:
                raise VoiceError("EDIT_AUDIO_INVALID", "编辑时音频文件变得不完整")
            if transform:
                values = _samples(raw, width)
                for i, value in enumerate(values):
                    position = done + i // channels
                    envelope = 1.0
                    if fade_in:
                        envelope = min(envelope, position / max(1, fade_in * rate))
                    if fade_out:
                        envelope = min(envelope, (count - 1 - position) / max(1, fade_out * rate))
                    scaled = round(value * gain * envelope)
                    clipped += int(scaled < minimum or scaled > maximum)
                    values[i] = max(minimum, min(maximum, scaled))
                raw = _encode(values, width)
            dest.writeframesraw(raw)
            done += frames
        if append:
            with wave.open(str(append), "rb") as extra:
                remaining = extra.getnframes()
                while remaining:
                    if cancelled and cancelled.is_set():
                        raise VoiceError("CANCELLED", "编辑已取消")
                    frames = min(16384, remaining)
                    raw = extra.readframes(frames)
                    if len(raw) != frames * channels * width:
                        raise VoiceError("EDIT_AUDIO_INVALID", "拼接音频的数据不完整")
                    dest.writeframesraw(raw)
                    remaining -= frames
    return {**inspect_wave(output), "clipped_samples": clipped}
