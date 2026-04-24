"""DVD scanning and ripping — lsdvd + dvdbackup + mkvmerge.

No SCSI, no makemkvcon. Uses libdvdread/libdvdcss for CSS decryption
through standard block device reads.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from glob import glob
from pathlib import Path

from strophalos.ripper.orchestrate import TitleRipFailed

# Per-stage timeouts for one DVD title.  Modern drives rip a single
# title in a few minutes; 20m for dvdbackup is the "give up, the disc
# is rotted" threshold (vs the 1h that previously hid stalled rips).
# mkvmerge is a remux of in-tmpdir VOBs so 5m is plenty.
_DVDBACKUP_TIMEOUT = 1200
_MKVMERGE_TIMEOUT = 300


def _parse_duration(duration_str: str) -> int:
    """Parse lsdvd duration like '01:14:42.467' to seconds."""
    m = re.match(r"(\d+):(\d+):([\d.]+)", duration_str)
    if m:
        return int(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
    return 0


def scan_dvd(device: str) -> tuple[str | None, dict[int, int], dict[int, int]]:
    """Scan a DVD via lsdvd. Returns (label, durations, chapters).

    durations: {title_id: seconds}
    chapters: {title_id: chapter_count}
    Title IDs are 0-indexed (lsdvd uses 1-indexed, we convert).

    Parses human-readable output (lsdvd JSON support is unreliable).
    """
    try:
        result = subprocess.run(
            ["lsdvd", device],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("  lsdvd timed out")
        return None, {}, {}
    except Exception as e:
        print(f"  lsdvd error: {e}")
        return None, {}, {}

    output = result.stderr + result.stdout
    label: str | None = None
    durations: dict[int, int] = {}
    chapters: dict[int, int] = {}

    for line in output.splitlines():
        line = line.strip()

        if line.startswith("Disc Title:"):
            label = line.split(":", 1)[1].strip()
            continue

        # "Title: 01, Length: 00:14:11.567 Chapters: 03, Cells: 03, ..."
        m = re.match(r"Title:\s*(\d+),\s*Length:\s*([\d:.]+)\s*Chapters:\s*(\d+)", line)
        if m:
            tid = int(m.group(1)) - 1  # 0-indexed
            durations[tid] = _parse_duration(m.group(2))
            chapters[tid] = int(m.group(3))

    return label, durations, chapters


def rip_dvd_title(device: str, title_num: int, output_dir: str) -> Path | None:
    """Rip a single DVD title via dvdbackup + mkvmerge.

    title_num is 0-indexed (converted to 1-indexed for dvdbackup).
    Returns the path to the output MKV, or None on failure.
    """
    dvd_title = title_num + 1  # dvdbackup is 1-indexed

    with tempfile.TemporaryDirectory(prefix="strophalos-dvd-") as tmpdir:
        # dvdbackup dumps to tmpdir/LABEL/VIDEO_TS/VTS_XX_*.VOB
        print(f"  dvdbackup: title {dvd_title}...", flush=True)
        try:
            result = subprocess.run(
                ["dvdbackup", "-i", device, "-t", str(dvd_title), "-o", tmpdir],
                capture_output=True,
                text=True,
                timeout=_DVDBACKUP_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(f"  dvdbackup timed out after {_DVDBACKUP_TIMEOUT}s — disc likely unreadable")
            return None
        if result.returncode != 0:
            print(f"  dvdbackup failed (rc={result.returncode}): {result.stderr.strip()}")
            return None

        # Find the VIDEO_TS directory dvdbackup created
        video_ts = None
        for root, dirs, _files in os.walk(tmpdir):
            if "VIDEO_TS" in dirs:
                video_ts = os.path.join(root, "VIDEO_TS")
                break

        if not video_ts:
            print("  dvdbackup: no VIDEO_TS in output")
            return None

        # Find the IFO for this title — mkvmerge reads DVD structure from it
        ifo = os.path.join(video_ts, f"VTS_{dvd_title:02d}_0.IFO")
        if os.path.isfile(ifo):
            source = ifo
        else:
            # Fallback: concatenate VOB files
            vobs = sorted(glob(os.path.join(video_ts, f"VTS_{dvd_title:02d}_[1-9]*.VOB")))
            if not vobs:
                print(f"  No VOB files found for title {dvd_title}")
                return None
            # mkvmerge can take multiple inputs with + syntax
            source = vobs[0]
            for vob in vobs[1:]:
                source += f" +{vob}"

        # Remux to MKV
        os.makedirs(output_dir, exist_ok=True)
        out_mkv = os.path.join(output_dir, f"dvd_t{title_num:02d}.mkv")
        print(f"  mkvmerge: → {os.path.basename(out_mkv)}", flush=True)

        # Build mkvmerge command
        if os.path.isfile(ifo):
            cmd = ["mkvmerge", "-o", out_mkv, ifo]
        else:
            vobs = sorted(glob(os.path.join(video_ts, f"VTS_{dvd_title:02d}_[1-9]*.VOB")))
            cmd = ["mkvmerge", "-o", out_mkv]
            for i, vob in enumerate(vobs):
                if i > 0:
                    cmd.append("+")
                cmd.append(vob)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=_MKVMERGE_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"  mkvmerge timed out after {_MKVMERGE_TIMEOUT}s")
            return None
        if result.returncode > 1:  # mkvmerge: 0=ok, 1=warnings, 2=error
            print(f"  mkvmerge failed (rc={result.returncode}): {result.stderr.strip()}")
            return None

        return Path(out_mkv)


def rip_dvd_titles(device: str, title_ids: list[int], output_dir: str) -> None:
    """Rip multiple DVD titles. Raises TitleRipFailed on any title failure
    so the orchestrator can abort the disc and clean up partial output."""
    os.makedirs(output_dir, exist_ok=True)
    for i, tid in enumerate(title_ids):
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] Ripping title {tid} ({i + 1}/{len(title_ids)})...", flush=True)
        t0 = time.time()
        mkv = rip_dvd_title(device, tid, output_dir)
        elapsed = int(time.time() - t0)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        dur = f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"
        if not mkv:
            print(f"  [{time.strftime('%H:%M:%S')}] title {tid} failed after {dur}")
            raise TitleRipFailed(tid)
        print(f"  [{time.strftime('%H:%M:%S')}] title {tid} done in {dur}", flush=True)
