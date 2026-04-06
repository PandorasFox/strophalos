"""MKV file utilities — duration probing, subtitle track inspection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from strophalos.types import SubtitleTrack


def get_mkv_duration(mkv_path: Path) -> float:
    """Get MKV duration in seconds. Tries mkvmerge, falls back to ffprobe."""
    # Try mkvmerge first
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

    # Fallback: ffprobe
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mkv_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        pass

    return 0.0


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
