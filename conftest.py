"""Project-wide pytest configuration: keep the temp tree from growing forever.

Why this file exists
--------------------
Every ``tmp_path`` / ``tmpdir`` fixture lands in
``%LOCALAPPDATA%\\Temp\\pytest-of-<user>\\``.  Windows' user-profile service
(ProfSvc) walks the whole Temp tree once at every logon, opening each file
about three times.  On this machine a single open costs 0.3-0.55 ms because of
the long file-system filter chain (FLTMGR + antivirus + fileinfo + luafv + Wof),
so the logon stall is roughly ``file count x 0.3-0.55 ms``:

    42 010 files  ->  23.1 s of black screen   (measured 2026-10-23)
     6 643 files  ->   2.0 s                   (measured 2026-10-23)

pytest itself only keeps the last three runs.  On Windows that cleanup fails
silently whenever a sqlite handle or a child process still holds a file:
pytest renames the old run to ``garbage-<uuid>`` and then cannot delete it.
620 runs left 35 369 files / 609 MB, 92 of them un-deletable ``garbage-*``
tombstones.

What this file does - and deliberately does not do
--------------------------------------------------
It does **not** move the temp root.  Moving it out of ``%TEMP%`` was tried and
reverted: ``%TEMP%`` is excluded from real-time antivirus scanning, so any other
location makes every file operation measurably slower, which flipped a
race-sensitive test in ``tests/test_workflow_retention.py``.  Changing where
tests run is not worth it to fix a cleanup problem.

Instead this sweeps the residues pytest failed to delete, at the start of every
session, before any test runs:

* ``garbage-*`` tombstones are removed (they exist only because a delete failed)
* numbered runs beyond the newest ``WB_PYTEST_KEEP_RUNS`` (default 2) are removed

Only directories older than ``WB_PYTEST_SWEEP_MIN_AGE`` seconds (default 3600)
are touched, so a second pytest running at the same time is never disturbed.
Everything here is best-effort: a failure to clean up must never fail a test.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest


def _keep_runs() -> int:
    try:
        return max(1, int(os.environ.get("WB_PYTEST_KEEP_RUNS", "2")))
    except ValueError:
        return 2


def _min_age() -> float:
    try:
        return max(0.0, float(os.environ.get("WB_PYTEST_SWEEP_MIN_AGE", "3600")))
    except ValueError:
        return 3600.0


def _force_writable(func, path, _exc_info):
    """rmtree error hook: clear the read-only bit and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _remove(path: Path) -> bool:
    for _ in range(3):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(str(path), onexc=lambda f, p, e: _force_writable(f, p, e))
            else:
                shutil.rmtree(str(path), onerror=_force_writable)
        except (OSError, TypeError):
            pass
        if not path.exists():
            return True
        time.sleep(0.2)
    return not path.exists()


def _run_number(name: str) -> int | None:
    if not name.startswith("pytest-"):
        return None
    try:
        return int(name[len("pytest-"):])
    except ValueError:
        return None


def _sweep_root(root: Path, min_age: float, keep: int) -> tuple[int, int]:
    """Return (removed, failed) counts for one ``pytest-of-<user>`` root."""
    removed = 0
    failed = 0
    now = time.time()

    def old_enough(p: Path) -> bool:
        try:
            return (now - p.stat().st_mtime) >= min_age
        except OSError:
            return False

    tombstones = []
    runs = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0, 0

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith("garbage-"):
            tombstones.append(entry)
        else:
            number = _run_number(entry.name)
            if number is not None:
                runs.append((number, entry))

    for entry in tombstones:
        if not old_enough(entry):
            continue
        if _remove(entry):
            removed += 1
        else:
            failed += 1

    runs.sort(key=lambda item: item[0])
    stale = runs[:-keep] if keep > 0 else runs
    for _number, entry in stale:
        if not old_enough(entry):
            continue
        if _remove(entry):
            removed += 1
        else:
            failed += 1

    return removed, failed


def pytest_sessionstart(session) -> None:
    keep = _keep_runs()
    min_age = _min_age()
    total_removed = 0
    total_failed = 0
    try:
        temp_root = Path(tempfile.gettempdir())
        for root in temp_root.glob("pytest-of-*"):
            if not root.is_dir():
                continue
            removed, failed = _sweep_root(root, min_age, keep)
            total_removed += removed
            total_failed += failed
    except Exception:  # never let housekeeping break a test run
        return

    if total_removed or total_failed:
        sys.stderr.write(
            "[conftest] swept %d stale pytest temp dir(s), %d could not be removed\n"
            % (total_removed, total_failed)
        )


@pytest.fixture(scope="session", autouse=True)
def _isolated_drag_cache(tmp_path_factory):
    """Dragging stages real copies; keep the suite out of the installed app's cache.

    ``drag_cache_dir`` defaults to ``%LOCALAPPDATA%\\声笺\\drag-cache`` - a folder the
    user's video projects point at.  Tests must never write there.
    """
    folder = tmp_path_factory.mktemp("drag-cache")
    previous = os.environ.get("SHENGJIAN_DRAG_CACHE")
    os.environ["SHENGJIAN_DRAG_CACHE"] = str(folder)
    try:
        yield folder
    finally:
        if previous is None:
            os.environ.pop("SHENGJIAN_DRAG_CACHE", None)
        else:
            os.environ["SHENGJIAN_DRAG_CACHE"] = previous
