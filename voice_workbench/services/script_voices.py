"""Resolve paragraph voice mentions without importing desktop components."""
from __future__ import annotations

from ..subtitles import parse_text


def utf16_length(text):
    """Qt document offsets count UTF-16 code units, including surrogate pairs."""
    return len(text.encode("utf-16-le")) // 2


def parse_script(text, voices, mentions=()):
    presets = [v for v in voices if not v.get("is_template")]
    by_id = {v["id"]: v for v in presets}
    bindings = {m["start"]: m for m in mentions or ()}
    if not any(line.lstrip().startswith("@") for line in text.splitlines()):
        return parse_text(text)
    if "-->" in text:
        raise ValueError("带时间轴的字幕暂不支持 @ 音色，请改为每行一句的文本后选择音色。")
    result, offset = [], 0
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.lstrip()
        start = offset + utf16_length(line[:len(line) - len(stripped)])
        offset += utf16_length(line) + 1
        if not stripped:
            continue
        voice_id = None
        if stripped.startswith("@@"):
            stripped = stripped[1:]  # An explicit escape for a literal leading @.
        elif stripped.startswith("@"):
            binding = bindings.get(start)
            if binding and stripped.startswith("@" + binding["label"]) and (
                    len(stripped) == len(binding["label"]) + 1 or stripped[len(binding["label"]) + 1].isspace()):
                voice = by_id.get(binding["preset_id"])
                if voice is None:
                    raise ValueError(f"第 {number} 行的声音预设「{binding['label']}」已删除，请重新输入 @ 选择。")
                length = len(binding["label"]) + 1
            else:
                matches = [v for v in presets if stripped.startswith("@" + v["name"]) and (
                    len(stripped) == len(v["name"]) + 1 or stripped[len(v["name"]) + 1].isspace())]
                if not matches:
                    raise ValueError(f"第 {number} 行没有匹配的声音预设，请在行首输入 @ 选择，音色与正文用空格分隔。")
                longest = max(len(v["name"]) for v in matches)
                matches = [v for v in matches if len(v["name"]) == longest]
                if len(matches) != 1:
                    raise ValueError(f"第 {number} 行存在同名声音预设，请通过 @ 列表明确选择音色。")
                voice, length = matches[0], longest + 1
            voice_id = voice["id"]
            stripped = stripped[length:].lstrip()
            if not stripped:
                raise ValueError(f"第 {number} 行已选择音色，请在空格后输入要生成的正文。")
        for segment in parse_text(stripped):
            if voice_id:
                segment["voice_id"] = voice_id
            result.append(segment)
    if not result:
        return parse_text("")
    return result
