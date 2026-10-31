"""Which audio files exist on disk, answered by one directory read.

Every record in the library used to ask the filesystem about its own file
(``Path.is_file``). On Windows each of those is a full open through the filter
chain -- measured 0.3-0.55 ms per file on a machine with antivirus drivers
installed, and the audio manager refreshed every 1.8 s. One ``scandir`` returns
the same answers for the whole folder in a single pass.
"""
from __future__ import annotations

import os
from pathlib import Path


def present_files(folder) -> set[str]:
    """Names of the files directly under *folder*; a missing folder is empty."""
    try:
        with os.scandir(folder) as listing:
            # DirEntry.is_file() is answered from the directory entry on Windows,
            # so this stays a single read for the whole folder.
            return {entry.name for entry in listing if entry.is_file()}
    except OSError:
        return set()


def present_audio(root) -> set[str]:
    """Names of the files in a data directory's ``audio`` folder."""
    return present_files(Path(root) / "audio")
