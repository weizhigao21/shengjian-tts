from __future__ import annotations

from pathlib import Path
import re


# 与 schemas.JobInput.segments 的上限一致；守卫测试会比对两处，改一处漏一处就报错。
POOL_LIMIT = 10000


def read_text(path) -> str:
    """按 UTF-8 读取字幕或文本，失败再按 GB18030 兜底。"""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030")


def build_pool(blocks, existing: str = "") -> tuple[str, int]:
    """把多份字幕拼成一个生成池，返回合并后的正文与总段数。

    池的意义不是排版：批量后端按「同一个任务」合批，而冷启动门槛是 6 条。
    一个字幕一个任务时每个任务只有三四行，永远凑不满，只能退回原生引擎。
    """
    parts = [block.strip() for block in blocks if block and block.strip()]
    if not parts:
        raise ValueError("没有可导入的文本")
    body = "\n\n".join(parts)
    merged = (existing.strip() + "\n\n" + body) if existing.strip() else body
    segments = len(parse_text(merged))
    if segments > POOL_LIMIT:
        raise ValueError(f"生成池已有 {segments} 段，超过单次可提交的 {POOL_LIMIT} 段；请分成两个池。")
    return merged, segments


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
                # 生成池里常把 SRT 和 TXT 拼在一起：没有时间轴的行照样要生成，
                # 否则混着导入时这些句子会被整块静默丢掉。每行仍算一段。
                # 但 WEBVTT 文件头与单独的序号块不是台词，必须跳过。
                head = lines[0].strip().upper() if lines else ""
                if head.startswith("WEBVTT") or all(line.strip().isdigit() for line in lines if line.strip()):
                    continue
                result.extend({"text": line.strip(), "timestamp": ""} for line in lines if line.strip())
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
