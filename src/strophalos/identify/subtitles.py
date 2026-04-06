"""Subtitle extraction — embedded text, PGS OCR, and external sources."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from strophalos.backends.whisper import transcribe
from strophalos.core.mkv import list_subtitle_tracks, select_best_track


def parse_srt(srt_text: str) -> list[tuple[float, str]]:
    """Parse SRT format into (timestamp_seconds, text) pairs."""
    results: list[tuple[float, str]] = []
    for match in re.finditer(r"(\d+):(\d+):([\d,]+)\s*-->.*?\n(.*?)(?:\n\n|\Z)", srt_text, re.DOTALL):
        h, m = int(match.group(1)), int(match.group(2))
        s = float(match.group(3).replace(",", "."))
        timestamp = h * 3600 + m * 60 + s
        line = re.sub(r"<[^>]+>", "", match.group(4)).strip().replace("\n", " ")
        if line:
            results.append((timestamp, line))
    return results


def extract_subtitles(mkv_path: Path, duration: float, *, full: bool = False) -> list[tuple[float, str]]:
    """Extract subtitle text from an MKV.

    Tries embedded subs (text or PGS+OCR), falls back to Whisper transcription.
    Returns (timestamp_seconds, text) pairs. When full=False (default), filters
    to the first 25% of duration; when full=True, returns all cues.
    """
    tracks = list_subtitle_tracks(mkv_path)
    track = select_best_track(tracks)
    if not track:
        return transcribe(mkv_path, duration)

    cutoff = duration * 0.25

    with tempfile.TemporaryDirectory() as tmpdir:
        sub_path = Path(tmpdir) / "subs"

        if track.is_text:
            # Text subs: extract directly
            ext = ".srt" if "SRT" in track.codec.upper() else ".ass"
            out = sub_path.with_suffix(ext)
            print(f"    subtitle: {track.codec} ({track.language}) [text]")
            try:
                subprocess.run(
                    ["mkvextract", "tracks", str(mkv_path), f"{track.track_id}:{out}"],
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                return []
            if not out.exists():
                return []

            text = out.read_text(errors="replace")
            if "ASS" in track.codec.upper() or "SSA" in track.codec.upper():
                results: list[tuple[float, str]] = []
                for match in re.finditer(r"Dialogue:\s*\d+,(\d+):(\d+):([\d.]+),.*?,.*?,.*?,.*?,.*?,.*?,(.*)", text):
                    h, m, s2 = int(match.group(1)), int(match.group(2)), float(match.group(3))
                    ts = h * 3600 + m * 60 + s2
                    line = re.sub(r"\{[^}]*\}", "", match.group(4)).strip()
                    if line:
                        results.append((ts, line))
            else:
                results = parse_srt(text)
        else:
            # PGS subs: extract .sup then OCR via pgsrip
            sup_path = sub_path.with_suffix(".sup")
            print(f"    subtitle: {track.codec} ({track.language}) [PGS→pgsrip]")
            try:
                subprocess.run(
                    ["mkvextract", "tracks", str(mkv_path), f"{track.track_id}:{sup_path}"],
                    capture_output=True,
                    timeout=60,
                )
            except Exception:
                return []
            if not sup_path.exists() or sup_path.stat().st_size == 0:
                return []

            # Run pgsrip to produce .srt
            try:
                subprocess.run(
                    ["python3", "-m", "pgsrip", str(sup_path)],
                    capture_output=True,
                    timeout=300,
                )
            except Exception:
                return []

            srt_path = sup_path.with_suffix(".srt")
            if not srt_path.exists():
                return []

            results = parse_srt(srt_path.read_text(errors="replace"))

    if not full and cutoff > 0:
        results = [(t, text) for t, text in results if t <= cutoff]

    # If embedded subs yielded nothing, try Whisper
    if not results:
        return transcribe(mkv_path, duration)

    return results
