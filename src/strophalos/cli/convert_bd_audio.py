"""CLI entry point for convert-bd-audio — convert audio BD MKVs to tagged FLACs.

Reads .rip-manifest.json for the MusicBrainz release ID, fetches track metadata,
extracts lossless audio from each MKV via ffmpeg, and writes tagged FLAC files.

Can be run against existing rip archives. Skips files that have already been converted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from strophalos.backends.musicbrainz import _mb_base, _get_release_tracks, get_english_name
from strophalos.core.fs import sanitize_filename
from strophalos.core.http import get_json


def _fetch_track_metadata(release_id: str) -> list[dict] | None:
    """Fetch track list with English names where available."""
    tracks = _get_release_tracks({}, release_id)
    if not tracks:
        return None

    # Enrich with English aliases
    enriched = []
    for t in tracks:
        rec_id = t.get("recording_id", "")
        title = t.get("title", "Unknown")

        # Try English alias for the recording title
        if rec_id:
            en_title = get_english_name("recording", rec_id, title)
        else:
            en_title = title

        enriched.append({
            "position": t.get("position", "?"),
            "title": en_title,
            "original_title": title,
            "recording_id": rec_id,
            "duration": t.get("duration", 0),
        })

    return enriched


def _fetch_release_artist(release_id: str) -> tuple[str, str]:
    """Get English artist name and MB artist ID for a release."""
    url = f"{_mb_base()}/ws/2/release/{release_id}?inc=artist-credits&fmt=json"
    data = get_json(url, headers={"User-Agent": "strophalos/1.0"}, timeout=10)
    if not data:
        return "Unknown", ""

    credits = data.get("artist-credit", [])
    if not credits:
        return "Unknown", ""

    artist_id = credits[0].get("artist", {}).get("id", "")
    default_name = credits[0].get("name", "Unknown")

    if artist_id:
        en_name = get_english_name("artist", artist_id, default_name)
    else:
        en_name = default_name

    return en_name, artist_id


def _convert_mkv_to_flac(mkv_path: Path, flac_path: Path) -> bool:
    """Extract audio from MKV to FLAC via ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mkv_path), "-vn", "-c:a", "flac", str(flac_path)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return result.returncode == 0


def _tag_flac(
    flac_path: Path,
    *,
    artist: str,
    album_artist: str,
    album: str,
    title: str,
    track_number: int,
    track_total: int,
    disc_number: int = 1,
    disc_total: int = 1,
    date: str = "",
    mb_release_id: str = "",
    mb_recording_id: str = "",
    mb_artist_id: str = "",
) -> bool:
    """Write Vorbis comment tags to a FLAC file."""
    tag_args = [
        f"ARTIST={artist}",
        f"ALBUMARTIST={album_artist}",
        f"ALBUM={album}",
        f"TITLE={title}",
        f"TRACKNUMBER={track_number}",
        f"TRACKTOTAL={track_total}",
        f"DISCNUMBER={disc_number}",
        f"DISCTOTAL={disc_total}",
    ]
    if date:
        tag_args.append(f"DATE={date}")
    if mb_release_id:
        tag_args.append(f"MUSICBRAINZ_ALBUMID={mb_release_id}")
    if mb_recording_id:
        tag_args.append(f"MUSICBRAINZ_TRACKID={mb_recording_id}")
    if mb_artist_id:
        tag_args.append(f"MUSICBRAINZ_ALBUMARTISTID={mb_artist_id}")

    cmd = ["metaflac", "--remove-all-tags"]
    for tag in tag_args:
        cmd.extend(["--set-tag", tag])
    cmd.append(str(flac_path))

    result = subprocess.run(cmd, capture_output=True, timeout=30)
    return result.returncode == 0


def convert_bd_audio(disc_dir: Path, output_dir: Path | None = None, dry_run: bool = False) -> int:
    """Convert audio BD MKVs to tagged FLACs.

    If output_dir is None, FLACs are written alongside the MKVs.
    Returns count of converted files.
    """
    manifest_path = disc_dir / ".rip-manifest.json"
    if not manifest_path.exists():
        print(f"No .rip-manifest.json in {disc_dir}")
        return 0

    manifest = json.loads(manifest_path.read_text())
    release_id = manifest.get("musicbrainz_release_id", "")
    if not release_id:
        print(f"No MusicBrainz release ID in manifest")
        return 0

    mb_track_count = manifest.get("mb_track_count", 0)
    album_title = manifest.get("musicbrainz_title", manifest.get("label", "Unknown"))

    print(f"Fetching track metadata for release {release_id}...")
    tracks = _fetch_track_metadata(release_id)
    if not tracks:
        print("  Failed to fetch track metadata from MusicBrainz")
        return 0

    print(f"  {len(tracks)} tracks from MusicBrainz")

    artist_name, artist_id = _fetch_release_artist(release_id)
    print(f"  Artist: {artist_name}")

    out = output_dir or disc_dir
    out.mkdir(parents=True, exist_ok=True)

    converted = 0
    for i, track in enumerate(tracks):
        mkv_name = f"track_t{i:02d}.mkv"
        mkv_path = disc_dir / mkv_name

        if not mkv_path.exists():
            print(f"  skip: {mkv_name} not found")
            continue

        pos = track["position"]
        title_safe = sanitize_filename(track["title"])
        flac_name = f"{pos:02d} - {title_safe}.flac" if isinstance(pos, int) else f"{pos} - {title_safe}.flac"
        flac_path = out / flac_name

        if flac_path.exists():
            print(f"  skip: {flac_name} already exists")
            continue

        if dry_run:
            print(f"  would convert: {mkv_name} → {flac_name}")
            converted += 1
            continue

        print(f"  converting: {mkv_name} → {flac_name}...", flush=True)
        if not _convert_mkv_to_flac(mkv_path, flac_path):
            print(f"    ffmpeg failed for {mkv_name}")
            continue

        _tag_flac(
            flac_path,
            artist=artist_name,
            album_artist=artist_name,
            album=album_title,
            title=track["title"],
            track_number=pos if isinstance(pos, int) else i + 1,
            track_total=len(tracks),
            mb_release_id=release_id,
            mb_recording_id=track.get("recording_id", ""),
            mb_artist_id=artist_id,
        )
        converted += 1

    print(f"{'Would convert' if dry_run else 'Converted'} {converted} file(s)")
    return converted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert audio BD MKVs to tagged FLACs"
    )
    parser.add_argument("path", help="Disc directory containing track_tNN.mkv files and .rip-manifest.json")
    parser.add_argument("-o", "--output", default=None, help="Output directory for FLACs (default: same as input)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be converted without writing")
    parser.add_argument("--all", action="store_true", help="Process all disc directories under the given path")
    args = parser.parse_args()

    target = Path(args.path)
    output = Path(args.output) if args.output else None

    if args.all:
        # Walk subdirectories looking for .rip-manifest.json
        for manifest in sorted(target.rglob(".rip-manifest.json")):
            disc_dir = manifest.parent
            manifest_data = json.loads(manifest.read_text())
            if manifest_data.get("disc_type") != "music":
                continue
            print(f"\n=== {disc_dir} ===")
            disc_output = (output / disc_dir.relative_to(target)) if output else None
            convert_bd_audio(disc_dir, disc_output, dry_run=args.dry_run)
    else:
        convert_bd_audio(target, output, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
