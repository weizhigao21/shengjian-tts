"""Audio/video suffixes the recognition mode accepts, shared by service and batch."""
from __future__ import annotations

# Formats the native reader and the Qt player both accept.
AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
# Video containers the bundled Qt/FFmpeg decoder opens; only the audio track is kept.
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".ts", ".m2ts",
                  ".mts", ".mpg", ".mpeg", ".3gp", ".ogv"}
ALLOWED_SUFFIXES = AUDIO_SUFFIXES | VIDEO_SUFFIXES


def dialog_filter() -> str:
    """One file-dialog string so every entry point accepts the same files."""
    return ("音频或视频 (" + " ".join("*" + suffix for suffix in sorted(ALLOWED_SUFFIXES)) + ");;"
            "音频 (" + " ".join("*" + suffix for suffix in sorted(AUDIO_SUFFIXES)) + ");;"
            "视频 (" + " ".join("*" + suffix for suffix in sorted(VIDEO_SUFFIXES)) + ")")
