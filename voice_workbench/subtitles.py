from __future__ import annotations

import re


def normalize_timestamp(timestamp: str, fallback_ms: int = 0) -> str:
    if timestamp:
        parts = timestamp.replace(",", ".").split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"时间戳格式无效：{timestamp}")
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[0]) if len(parts) == 3 else 0
        total = round((hours * 3600 + minutes * 60 + seconds) * 100)
    else:
        total = round(fallback_ms / 10)
    hours, remainder = divmod(total, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def parse_text(content: str) -> list[dict]:
    content = content.lstrip("\ufeff").replace("\r\n", "\n")
    result = []
    if "-->" in content:
        for block in re.split(r"\n\s*\n", content.strip()):
            lines = block.strip().splitlines()
            timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
            if timing is None:
                continue
            text = " ".join(lines[timing + 1:]).strip()
            if text:
                result.append({"text": text, "timestamp": lines[timing].split("-->")[0].strip()})
    elif re.search(r"\[\d+:\d+(?:\.\d+)?\]", content):
        for line in content.splitlines():
            stamps = re.findall(r"\[(\d+:\d+(?:\.\d+)?)\]", line)
            text = re.sub(r"\[[^\]]*\]", "", line).strip()
            if text:
                result.extend({"text": text, "timestamp": stamp} for stamp in stamps)
    else:
        result = [{"text": line.strip(), "timestamp": ""} for line in content.splitlines() if line.strip()]
    if not result:
        raise ValueError("未找到可生成的文本。支持 TXT（每行一句）、LRC、SRT 和 VTT。")
    return result
