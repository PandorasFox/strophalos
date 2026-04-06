"""CLI entry point for identify-music — MusicBrainz lookup + hard-link to library."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from strophalos.backends.musicbrainz import search_release
from strophalos.core.fs import sanitize_filename
from strophalos.core.mkv import get_mkv_duration
from strophalos.core.notify import notify


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
    release = search_release(args.label, durations)

    if not release:
        clean = args.label.replace("_", " ")
        msg = f"No MusicBrainz match for '{clean}'. Check https://musicbrainz.org"
        print(f"  {msg}")
        notify(f"{clean}: music identification failed", msg, error=True)
        return

    artist = sanitize_filename(release["artist"])
    album = sanitize_filename(release["title"])
    tracks = release["tracks"]

    # Match files to tracks by duration (sort both, pair up)
    files_by_dur = sorted(file_durs, key=lambda x: x[1])
    tracks_by_dur = sorted(enumerate(tracks), key=lambda x: x[1]["duration"])

    track_to_file: dict[int, Path] = {}
    for (mkv, _dur), (track_idx, _track) in zip(files_by_dur, tracks_by_dur, strict=True):
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
            notify(f"{artist} - {album}: link conflict", f"{link_path}", error=True)
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
                {
                    "position": t["position"],
                    "title": t["title"],
                    "file": track_to_file.get(i, Path()).name,
                }
                for i, t in enumerate(tracks)
            ],
            "timestamp": datetime.now().isoformat(),
        }
        manifest_path = disc_dir / ".music-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  Manifest written: {manifest_path}")

    mb_url = f"https://musicbrainz.org/release/{release['id']}"
    body = f"{artist} - {album}\n{mb_url}\n{linked} track(s) linked"
    notify(f"Audio BD linked: {artist} - {album}", body)
    print(f"Done: {artist} - {album}")


if __name__ == "__main__":
    main()
