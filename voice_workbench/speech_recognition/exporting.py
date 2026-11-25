"""Write recognition lines as SRT/TXT beside the source file.

Batch recognition exports every finished transcript to the folder that holds the
audio or video it came from. Existing files are never overwritten blindly: an
identical file is reused, and a different file with the same name keeps its
content while ours becomes ``<name>.识别字幕.<ext>``. That makes a re-run
idempotent without destroying subtitles the user has already edited.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..errors import VoiceError
from ..subtitles import normalize_timestamp

MAX_STEM_CHARS = 160
INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def srt_text(lines) -> str:
    """SRT blocks for lines with ``index``/``start``/``end``/``text``."""
    blocks = []
    for position, line in enumerate(lines, start=1):
        index = line.get("index") or position
        start = max(0.0, float(line.get("start") or 0))
        end = max(start, float(line.get("end") or 0))
        blocks.append(f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{line.get('text') or ''}")
    return "\n\n".join(blocks) + "\n"


def plain_text(lines) -> str:
    return "\n".join(line.get("text") or "" for line in lines)


def timestamp(seconds) -> str:
    return normalize_timestamp("", round(float(seconds or 0) * 1000)).replace(".", ",")


def is_chinese(target) -> bool:
    """``zh``, ``zh-CN`` and ``zh_TW`` all mean a Chinese subtitle."""
    value = str(target or "").strip().lower().replace("_", "-")
    return value.split("-")[0] == "zh"


def translated_lines(translation):
    """SRT-ready lines from a translation dict, skipping untranslated rows."""
    result = []
    for position, line in enumerate((translation or {}).get("lines") or [], start=1):
        text = str(line.get("translated") or "").strip()
        if not text:
            continue
        result.append({"index": line.get("index") or position,
                       "start": line.get("start"), "end": line.get("end"), "text": text})
    return result


def export_transcript(state, folder=None, formats="srt", stem=None):
    """Export one recognition session and return the files written.

    ``folder`` defaults to the directory of ``source_path``; ``formats`` may be
    ``srt``, ``txt`` or ``srt+txt``. A missing folder or an unwritable target is
    reported as :class:`VoiceError` so a batch can record it per item and keep
    going with the next file.
    """
    lines = state.get("lines") or []
    if not lines:
        raise VoiceError("RECOGNITION_EMPTY", "识别结果为空，没有可导出的字幕",
                         "确认音频中有清晰语音，或等识别完成后再导出。", "导出字幕")
    return _export_lines(lines, state, folder=folder, formats=formats, stem=stem)


def export_translation(state, folder=None, formats="srt", stem=None):
    """Export only the translated text, with the original timings."""
    lines = translated_lines(state.get("translation"))
    if not lines:
        raise VoiceError("TRANSLATION_EMPTY", "这条记录还没有译文，无法导出中文字幕",
                         "先翻译，或让 Agent 回写译文后再导出。", "导出字幕")
    translation = state.get("translation") or {}
    target = str(translation.get("target") or "译文")
    if stem is None:
        source = Path(state.get("source_path") or "")
        base = source.stem or (state.get("name") or "识别结果")
        stem = f"{base}.{target}"
    return _export_lines(lines, state, folder=folder, formats=formats, stem=stem)


def _export_lines(lines, state, folder=None, formats="srt", stem=None):
    if folder is None:
        source = state.get("source_path") or ""
        if not source:
            raise VoiceError("EXPORT_FOLDER_MISSING", "识别记录没有原文件路径，无法判断导出位置",
                             "手动选择保存位置导出。", "导出字幕")
        folder = Path(source).parent
    folder = Path(folder)
    if not folder.is_dir():
        raise VoiceError("EXPORT_FOLDER_MISSING", f"字幕要放到的文件夹不存在：{folder}",
                         "把音频移回可写的文件夹，或手动选择保存位置后重试。", "导出字幕")
    if stem is None:
        source = Path(state.get("source_path") or "")
        stem = source.stem or (state.get("name") or "识别结果")
    stem = _safe_stem(stem)
    kinds = _kinds(formats)
    written = []
    for kind in kinds:
        content = srt_text(lines) if kind == "srt" else plain_text(lines)
        target = _target(folder, stem, "." + kind, content)
        try:
            with target.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        except OSError as exc:
            raise VoiceError("EXPORT_FAILED", f"无法写入字幕文件：{target}（{exc}）",
                             "确认文件夹可写，且字幕文件没有被其他程序占用。", "导出字幕") from exc
        written.append(str(target))
    return written


def _kinds(formats):
    value = str(formats or "srt").lower()
    if value in ("srt+txt", "both", "srt,txt"):
        return ("srt", "txt")
    if value == "txt":
        return ("txt",)
    return ("srt",)


def _safe_stem(stem):
    stem = INVALID_NAME.sub("_", str(stem or "识别结果")).strip() or "识别结果"
    return stem[:MAX_STEM_CHARS]


def _target(folder, stem, extension, content):
    preferred = Path(folder) / f"{stem}{extension}"
    if not preferred.exists():
        return preferred
    try:
        if preferred.read_text(encoding="utf-8-sig") == content:
            return preferred
    except (OSError, UnicodeDecodeError):
        pass
    # Same name, different content: keep the user's file and write ours beside it.
    return Path(folder) / f"{stem}.识别字幕{extension}"
