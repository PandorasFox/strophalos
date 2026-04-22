"""MKV file utilities — duration probing, subtitle track inspection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from strophalos.types import SubtitleTrack

_EBML_MAGIC = b"\x1a\x45\xdf\xa3"


def is_valid_mkv(mkv_path: Path) -> bool:
    """Check if a file starts with a valid EBML header (Matroska/WebM).

    MakeMKV writes ``This file was not properly finalized`` when a rip
    fails or is interrupted — this catches those broken outputs cheaply.
    """
    try:
        with open(mkv_path, "rb") as f:
            return f.read(4) == _EBML_MAGIC
    except Exception:
        return False


def get_mkv_duration(mkv_path: Path) -> float:
    """Get MKV duration in seconds via ffprobe, falling back to mkvmerge.

    Uses ``-f matroska`` to force the Matroska demuxer.  Without it,
    Fedora's ffmpeg (which ships with HEVC decoding disabled) can
    misidentify HEVC MKV files as raw EAC3 audio and estimate duration
    from filesize/bitrate, producing wildly inflated values.
    """
    if not is_valid_mkv(mkv_path):
        return 0.0

    # Primary: ffprobe with forced Matroska demuxer
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-f",
                "matroska",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mkv_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        val = result.stdout.strip()
        if val and val != "N/A":
            dur = float(val)
            if dur > 0:
                return dur
    except Exception:
        pass

    # Fallback: mkvmerge
    try:
        result = subprocess.run(
            ["mkvmerge", "--identify", "--identification-format", "json", str(mkv_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(result.stdout)
        ns = info.get("container", {}).get("properties", {}).get("duration", 0)
        if ns > 0:
            return ns / 1_000_000_000
    except Exception:
        pass

    return 0.0


def repair_mkv(mkv_path: Path) -> bool:
    """Try to repair a corrupt MKV whose EBML header was overwritten.

    MakeMKV clobbers the first ~48 bytes with an error message when a rip
    fails, but the Matroska clusters (raw A/V data) are usually intact.
    We scan for the first Matroska Segment element and try to remux from
    there using ffmpeg (``-c copy``, no re-encoding).

    Returns True if the file was repaired in-place, False otherwise.
    """
    # Find the Segment element (EBML ID 0x18538067) — it starts right
    # after the (now-clobbered) EBML header, usually within the first 4 KB.
    _SEGMENT_ID = b"\x18\x53\x80\x67"
    try:
        with open(mkv_path, "rb") as f:
            head = f.read(8192)
        offset = head.find(_SEGMENT_ID)
        if offset < 0:
            return False
    except Exception:
        return False

    # Build a minimal EBML header that points to the existing Segment.
    # 0x1A45DFA3 = EBML element, containing DocType "matroska".
    ebml_header = (
        b"\x1a\x45\xdf\xa3"  # EBML ID
        b"\x01\x00\x00\x00\x00\x00\x00\x1f"  # size = 31 bytes
        b"\x42\x86\x81\x01"  # EBMLVersion = 1
        b"\x42\xf7\x81\x01"  # EBMLReadVersion = 1
        b"\x42\xf2\x81\x04"  # EBMLMaxIDLength = 4
        b"\x42\xf3\x81\x08"  # EBMLMaxSizeLength = 8
        b"\x42\x82\x88\x6d\x61\x74\x72\x6f\x73\x6b\x61"  # DocType = "matroska"
        b"\x42\x87\x81\x04"  # DocTypeVersion = 4
        b"\x42\x85\x81\x02"  # DocTypeReadVersion = 2
    )

    repaired = mkv_path.with_suffix(".repair.mkv")
    try:
        # Write new header + original data from Segment onward
        with open(mkv_path, "rb") as src, open(repaired, "wb") as dst:
            dst.write(ebml_header)
            src.seek(offset)
            while True:
                chunk = src.read(16 * 1024 * 1024)  # 16 MB chunks
                if not chunk:
                    break
                dst.write(chunk)

        # Verify the repaired file is parseable and remux to fix cues/index
        remuxed = mkv_path.with_suffix(".remux.mkv")
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(repaired), "-c", "copy", str(remuxed)],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.returncode != 0 or not is_valid_mkv(remuxed):
            return False

        # Replace the corrupt file with the remuxed output
        mkv_path.unlink()
        remuxed.rename(mkv_path)
        return True
    except Exception:
        return False
    finally:
        for tmp in (repaired, remuxed):
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def split_by_chapters(mkv_path: Path, output_dir: Path, prefix: str = "ch") -> list[Path]:
    """Split an MKV by chapter boundaries into individual files.

    Uses mkvmerge --split chapters:all. Returns sorted list of output file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / f"{prefix}-%03d.mkv")
    result = subprocess.run(
        ["mkvmerge", "-o", pattern, "--split", "chapters:all", str(mkv_path)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode > 1:  # mkvmerge: 0=ok, 1=warnings, 2=error
        print(f"  mkvmerge split failed (rc={result.returncode}): {result.stderr.strip()}")
        return []
    return sorted(output_dir.glob(f"{prefix}-*.mkv"))


def list_subtitle_tracks(mkv_path: Path) -> list[SubtitleTrack]:
    """List subtitle tracks in an MKV file."""
    try:
        result = subprocess.run(
            ["mkvmerge", "--identify", "--identification-format", "json", str(mkv_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(result.stdout)
    except Exception:
        return []

    tracks: list[SubtitleTrack] = []
    for track in info.get("tracks", []):
        if track.get("type") != "subtitles":
            continue
        props = track.get("properties", {})
        codec = props.get("codec_id", "")
        lang = props.get("language", "und")
        name = (props.get("track_name") or "").lower()
        forced = props.get("forced_track", False)

        # Skip forced/signs-only
        if forced or "sign" in name or "song" in name:
            continue

        is_text = any(t in codec.upper() for t in ("SRT", "ASS", "SSA", "UTF8", "TEXT"))

        tracks.append(
            SubtitleTrack(
                track_id=track["id"],
                codec=codec,
                language=lang,
                name=name,
                is_text=is_text,
            )
        )

    return tracks


def select_best_track(tracks: list[SubtitleTrack]) -> SubtitleTrack | None:
    """Pick the best subtitle track: prefer text, then PGS; prefer English.

    For PGS tracks, prefer the FIRST matching track — full dialog subs are
    typically listed before signs/songs/OP lyrics tracks.
    """
    if not tracks:
        return None

    def sort_key(t: SubtitleTrack) -> tuple[int, int, int, int]:
        is_eng = 1 if t.language in ("eng", "en") else 0
        is_text = 1 if t.is_text else 0
        is_und = 1 if t.language == "und" else 0
        # Prefer earlier track ID (lower = first in file = usually full subs)
        earlier = -t.track_id
        return (is_text, is_eng, is_und, earlier)

    return max(tracks, key=sort_key)
