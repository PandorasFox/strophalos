"""Per-disc rip orchestration shared by the BD and DVD backends.

The backend modules (`rip.py`, `dvd.py`) handle the protocol-specific work —
makemkvcon for BD/UHD, dvdbackup+mkvmerge for DVD — and signal hard failure
by raising `TitleRipFailed`.  This module provides the shared lifecycle:
abort the disc on first title failure (cleaning up the partial output so a
re-rip starts fresh), then validate/repair the produced MKVs.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from strophalos.core.mkv import is_valid_mkv, repair_mkv


class TitleRipFailed(Exception):
    """A title failed to produce an output file.

    Raised by backends after their own internal retries are exhausted.
    Callers should treat this as terminal for the disc — partial output
    is useless and the disc needs cleaning or a different drive.
    """

    def __init__(self, tid: int) -> None:
        super().__init__(f"title {tid} failed")
        self.tid = tid


def run_rip(rip_fn: Callable[[], None], output_dir: str) -> bool:
    """Run a per-disc rip callable; on `TitleRipFailed`, wipe the output dir.

    Returns True if every title succeeded, False if the rip aborted (in which
    case `output_dir` has been removed).
    """
    try:
        rip_fn()
        return True
    except TitleRipFailed as e:
        print(f"  Disc rip aborted: {e}. Cleaning up {output_dir}.")
        shutil.rmtree(output_dir, ignore_errors=True)
        return False


def validate_rip(output_dir: str) -> int | None:
    """Validate (and repair if possible) every MKV in `output_dir`.

    MakeMKV can exit 0 yet write a file with a clobbered EBML header
    ("not properly finalized") — the stream is intact, only the first
    ~48 bytes are wrong, so an ffmpeg remux fixes it.  Files that can't
    be repaired are removed.

    Returns the count of usable files, or None if every file was
    unrecoverable (caller should treat as a failed rip).
    """
    ripped_files = sorted(Path(output_dir).glob("*.mkv"))
    corrupt = [f for f in ripped_files if not is_valid_mkv(f)]
    if not corrupt:
        return len(ripped_files)

    still_bad: list[Path] = []
    for f in corrupt:
        print(f"  CORRUPT: {f.name} (missing EBML header)")
        if repair_mkv(f):
            print(f"  REPAIRED: {f.name}")
        else:
            print(f"  UNRECOVERABLE: {f.name} — removing")
            f.unlink()
            still_bad.append(f)

    valid_count = len(ripped_files) - len(still_bad)
    if valid_count == 0:
        print(f"  All {len(corrupt)} ripped file(s) are corrupt and unrecoverable — rip failed")
        return None
    if still_bad:
        print(f"  {valid_count} of {len(ripped_files)} file(s) usable")
    return valid_count
