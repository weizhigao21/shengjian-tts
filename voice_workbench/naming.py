"""Stable names for works that have not been named by the user."""
from datetime import datetime
import re

from .subtitles import parse_text

UNNAMED = {"", "未命名配音", "未命名作品", "配音任务"}


def work_type(job):
    return (job.get("editor_state") or {}).get("mode", "article")


def display_title(job):
    title = job.get("title") or "未命名配音"
    if job.get("auto_named"):
        entries = (job.get("editor_state") or {}).get("dialogue_entries", [])
        if work_type(job) == "dialogue" and entries:
            text = "".join(e.get("text", "") for e in entries)
        else:
            text = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*·\s*", "", title)
        title = re.sub(r"\s+", "", text)[:5] or "未命名配音"
    return title


def work_title(title, script, created, segments=()):
    if title.strip() not in UNNAMED:
        return title.strip()
    try:
        source = parse_text(script) if script and script.strip() else segments
    except ValueError:
        source = segments
    text = "".join(s["text"] for s in source)
    prefix = re.sub(r"\s+", "", text)[:5]
    return datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S") + " · " + prefix if prefix else "未命名配音"
