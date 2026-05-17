"""Subtitle extraction — embedded text, PGS OCR, and external sources."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from strophalos.backends.whisper import transcribe
from strophalos.core.mkv import list_subtitle_tracks, select_best_track
from strophalos.identify.srt import parse_srt


def extract_subtitles(mkv_path: Path, duration: float, *, full: bool = False) -> list[tuple[float, str]]:
    """Extract subtitle text from an MKV.

    Tries embedded subs (text or PGS+OCR), falls back to Whisper transcription.
    Returns (timestamp_seconds, text) pairs. When full=False (default), filters
    to the first 25% of duration; when full=True, returns all cues.
    """
    tracks = list_subtitle_tracks(mkv_path)
    track = select_best_track(tracks)
    if not track:
        print(f"    subtitle: no embedded subtitle tracks in {mkv_path.name}, trying whisper")
        return transcribe(mkv_path, duration)

    cutoff = duration * 0.25

    results: list[tuple[float, str]] = []

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
                pass
            if out.exists():
                text = out.read_text(errors="replace")
                if "ASS" in track.codec.upper() or "SSA" in track.codec.upper():
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
                pass
            if sup_path.exists() and sup_path.stat().st_size > 0:
                # Run pgsrip to produce .srt
                try:
                    subprocess.run(
                        ["python3", "-m", "pgsrip", str(sup_path)],
                        capture_output=True,
                        timeout=300,
                    )
                except Exception:
                    pass

                srt_path = sup_path.with_suffix(".srt")
                if srt_path.exists():
                    results = parse_srt(srt_path.read_text(errors="replace"))

    if not full and cutoff > 0:
        results = [(t, text) for t, text in results if t <= cutoff]

    # If embedded subs yielded nothing, fall back to Whisper
    if not results:
        print(f"    subtitle: embedded extraction yielded 0 cues for {mkv_path.name}, trying whisper")
        return transcribe(mkv_path, duration)

    print(f"    subtitle: {len(results)} embedded cue(s) from {mkv_path.name}")
    return results
