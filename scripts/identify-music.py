#!/usr/bin/env python3
"""Post-rip audio BD identification — hard-links tracks to the library.

Searches MusicBrainz by disc label, matches track durations, and creates
hard links in the library structure:
  /media/music/{Artist}/{Album}/01 - Track Title.mkv

Usage: identify-music.py --dir /media/archive/music/rips/bd/LABEL/disc1 --label LABEL
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

NOTIFY_SCRIPT = shutil.which("notify.sh") or "/usr/local/bin/notify.sh"


def _notify(title: str, body: str, error: bool = False) -> None:
    try:
        cmd = [NOTIFY_SCRIPT]
        if error:
            cmd.append("--error")
        cmd.extend([title, body])
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass


def sanitize_filename(name: str) -> str:
    name = name.replace(":", " -")
    name = re.sub(r'[?*<>|"\\]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def get_mkv_duration(mkv_path: Path) -> float:
    """Get MKV duration in seconds."""
    try:
        result = subprocess.run(
            ["mkvmerge", "--identify", "--identification-format", "json", str(mkv_path)],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(result.stdout)
        ns = info.get("container", {}).get("properties", {}).get("duration", 0)
        if ns > 0:
            return ns / 1_000_000_000
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(mkv_path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0


def mb_search_release(label: str, durations: list[float]) -> dict[str, Any] | None:
    """Search MusicBrainz for a release matching the disc label + track durations."""
    import urllib.parse
    import urllib.request

    mb_server = os.environ.get("MB_SERVER", "mb-web:5000")
    query = label.replace("_", " ").strip()
    if not query:
        return None

    url = (f"http://{mb_server}/ws/2/release/"
           f"?query=release:{urllib.parse.quote(query)}&fmt=json&limit=10")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "strophalos/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  MusicBrainz: search failed ({e})")
        return None

    releases = data.get("releases", [])
    if not releases:
        print(f"  MusicBrainz: no results for '{query}'")
        return None

    n_tracks = len(durations)
    sorted_durs = sorted(durations)

    for rel in releases:
        rel_id = rel.get("id", "")
        rel_title = rel.get("title", "")
        artist = ""
        if rel.get("artist-credit"):
            artist = rel["artist-credit"][0].get("name", "")

        # Fetch full release with recordings
        try:
            detail_url = (f"http://{mb_server}/ws/2/release/{rel_id}"
                          f"?inc=recordings+media+artist-credits&fmt=json")
            req = urllib.request.Request(detail_url, headers={"User-Agent": "strophalos/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                detail = json.loads(resp.read())
        except Exception:
            continue

        # Extract artist from detail if not in search result
        if not artist and detail.get("artist-credit"):
            artist = detail["artist-credit"][0].get("artist", {}).get("name", "")

        # Build track list with durations
        tracks: list[dict[str, Any]] = []
        for medium in detail.get("media", []):
            for track in medium.get("tracks", []):
                rec = track.get("recording", {})
                length_ms = rec.get("length") or track.get("length")
                tracks.append({
                    "position": track.get("position", track.get("number", "?")),
                    "title": rec.get("title", track.get("title", "?")),
                    "recording_id": rec.get("id", ""),
                    "duration": int(length_ms) / 1000.0 if length_ms else 0.0,
                })

        if len(tracks) != n_tracks:
            continue

        # Check duration alignment
        track_durs = sorted(t["duration"] for t in tracks)
        total_diff = sum(abs(a - b) for a, b in zip(sorted_durs, track_durs))
        avg_diff = total_diff / n_tracks

        if avg_diff < 5.0:
            print(f"  MusicBrainz: {artist} - {rel_title} ({len(tracks)} tracks, avg diff {avg_diff:.1f}s)")
            return {
                "id": rel_id,
                "title": rel_title,
                "artist": artist,
                "tracks": tracks,
            }

    print(f"  MusicBrainz: no duration match found")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Audio BD identification — hard-link to library")
    parser.add_argument("--dir", required=True, help="Archive disc directory")
    parser.add_argument("--label", required=True, help="Disc label")
    parser.add_argument("--library", default="/media", help="Library root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    disc_dir = Path(args.dir)
    library = Path(args.library)

    if not disc_dir.is_dir():
        print(f"Directory not found: {disc_dir}")
        return

    mkv_files = sorted(disc_dir.glob("*_t[0-9][0-9].mkv"))
    if not mkv_files:
        print("No MKV files found")
        return

    print(f"Found {len(mkv_files)} MKV file(s)")

    # Get durations for matching
    file_durs: list[tuple[Path, float]] = []
    for mkv in mkv_files:
        dur = get_mkv_duration(mkv)
        file_durs.append((mkv, dur))
        print(f"  {mkv.name}: {dur:.0f}s")

    # Search MusicBrainz
    durations = [d for _, d in file_durs]
    release = mb_search_release(args.label, durations)

    if not release:
        clean = args.label.replace("_", " ")
        msg = f"No MusicBrainz match for '{clean}'. Check https://musicbrainz.org"
        print(f"  {msg}")
        _notify(f"{clean}: music identification failed", msg, error=True)
        return

    artist = sanitize_filename(release["artist"])
    album = sanitize_filename(release["title"])
    tracks = release["tracks"]

    # Match files to tracks by duration (sort both, pair up)
    files_by_dur = sorted(file_durs, key=lambda x: x[1])
    tracks_by_dur = sorted(enumerate(tracks), key=lambda x: x[1]["duration"])

    # Build mapping: track index → file
    track_to_file: dict[int, Path] = {}
    for (mkv, _dur), (track_idx, _track) in zip(files_by_dur, tracks_by_dur):
        track_to_file[track_idx] = mkv

    # Link in track order
    album_dir = library / "music" / artist / album
    linked = 0

    for i, track in enumerate(tracks):
        mkv = track_to_file.get(i)
        if not mkv:
            continue

        pos = track["position"]
        title_safe = sanitize_filename(track["title"])
        new_name = f"{pos:02d} - {title_safe}.mkv" if isinstance(pos, int) else f"{pos} - {title_safe}.mkv"
        link_path = album_dir / new_name

        if link_path.exists():
            if link_path.stat().st_ino == mkv.stat().st_ino:
                print(f"  skip (already linked): {new_name}")
                continue
            print(f"  conflict: {new_name} exists with different inode")
            _notify(f"🚫🔗 {artist} - {album}: link conflict", f"{link_path}", error=True)
            continue

        action = "would link" if args.dry_run else "link"
        print(f"  {action}: {mkv.name} → {new_name}")

        if not args.dry_run:
            album_dir.mkdir(parents=True, exist_ok=True)
            os.link(mkv, link_path)
            linked += 1

    # Manifest
    if not args.dry_run and linked:
        manifest = {
            "artist": release["artist"],
            "album": release["title"],
            "musicbrainz_id": release["id"],
            "tracks": [
                {"position": t["position"], "title": t["title"],
                 "file": track_to_file.get(i, Path()).name}
                for i, t in enumerate(tracks)
            ],
            "timestamp": datetime.now().isoformat(),
        }
        manifest_path = disc_dir / ".music-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  Manifest written: {manifest_path}")

    mb_url = f"https://musicbrainz.org/release/{release['id']}"
    body = f"{artist} - {album}\n{mb_url}\n{linked} track(s) linked"
    _notify(f"Audio BD linked: {artist} - {album}", body)
    print(f"Done: {artist} - {album}")


if __name__ == "__main__":
    main()
