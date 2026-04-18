"""CLI entry point for retag-flac — re-tag FLACs with English locale names from MusicBrainz.

Reads existing MUSICBRAINZ_* tags from FLAC files, queries MusicBrainz for
English aliases of artists/releases/recordings, and rewrites the name tags
while preserving all MB IDs and other metadata.

Can be run as a post-rip step or against existing rip archives.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from strophalos.backends.musicbrainz import get_english_name as _get_english_name


def _get_recording_title(recording_id: str, fallback: str) -> str:
    """Get English title for a recording."""
    return _get_english_name("recording", recording_id, fallback)


def _get_artist_name(artist_id: str, fallback: str) -> str:
    """Get English name for an artist."""
    return _get_english_name("artist", artist_id, fallback)


def _get_release_title(release_id: str, fallback: str) -> str:
    """Get English title for a release."""
    return _get_english_name("release", release_id, fallback)


def _get_work_title(work_id: str, fallback: str) -> str:
    """Get English title for a work (used for COMPOSER-related lookups)."""
    return _get_english_name("work", work_id, fallback)


def _read_flac_tags(flac_path: Path) -> dict[str, list[str]]:
    """Read all Vorbis comments from a FLAC file."""
    result = subprocess.run(
        ["metaflac", "--export-tags-to=-", str(flac_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    tags: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        tags.setdefault(key.upper(), []).append(val)
    return tags


def _write_flac_tags(flac_path: Path, tags: dict[str, list[str]]) -> bool:
    """Write Vorbis comments to a FLAC file (replaces all existing tags)."""
    tag_args = []
    for key, values in tags.items():
        for val in values:
            tag_args.extend(["--set-tag", f"{key}={val}"])

    result = subprocess.run(
        ["metaflac", "--remove-all-tags", str(flac_path)],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        return False

    result = subprocess.run(
        ["metaflac"] + tag_args + [str(flac_path)],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def retag_flac(flac_path: Path, dry_run: bool = False) -> bool:
    """Re-tag a single FLAC with English names from MusicBrainz.

    Returns True if tags were changed.
    """
    tags = _read_flac_tags(flac_path)

    changed = False
    new_tags = dict(tags)

    # Artist
    artist_ids = tags.get("MUSICBRAINZ_ARTISTID", [])
    if artist_ids and "ARTIST" in tags:
        en_name = _get_artist_name(artist_ids[0], tags["ARTIST"][0])
        if en_name != tags["ARTIST"][0]:
            new_tags["ARTIST"] = [en_name]
            changed = True

    # Album artist (may have multiple IDs for collaborations)
    albumartist_ids = tags.get("MUSICBRAINZ_ALBUMARTISTID", [])
    if albumartist_ids and "ALBUMARTIST" in tags:
        en_names = []
        for aid in albumartist_ids:
            en_names.append(_get_artist_name(aid, ""))
        en_combined = " & ".join(n for n in en_names if n)
        if en_combined and en_combined != tags["ALBUMARTIST"][0]:
            new_tags["ALBUMARTIST"] = [en_combined]
            changed = True

    # Track title (via recording ID)
    track_id = tags.get("MUSICBRAINZ_TRACKID", [""])[0]
    if track_id and "TITLE" in tags:
        en_title = _get_recording_title(track_id, tags["TITLE"][0])
        if en_title != tags["TITLE"][0]:
            new_tags["TITLE"] = [en_title]
            changed = True

    # Album title (via release ID)
    album_id = tags.get("MUSICBRAINZ_ALBUMID", [""])[0]
    if album_id and "ALBUM" in tags:
        en_album = _get_release_title(album_id, tags["ALBUM"][0])
        if en_album != tags["ALBUM"][0]:
            new_tags["ALBUM"] = [en_album]
            changed = True

    # Composer (via work ID if available)
    work_id = tags.get("MUSICBRAINZ_WORKID", [""])[0]
    if work_id and "COMPOSER" in tags:
        # Work doesn't have an English "name" alias in the same way,
        # but the composer artist might
        pass  # Composer re-tagging would need artist lookup from the work's relations

    if not changed:
        return False

    if dry_run:
        for key in ("ARTIST", "ALBUMARTIST", "TITLE", "ALBUM"):
            old = tags.get(key, [""])[0]
            new = new_tags.get(key, [""])[0]
            if old != new:
                print(f"    {key}: {old} → {new}")
        return True

    return _write_flac_tags(flac_path, new_tags)


def retag_directory(directory: Path, dry_run: bool = False) -> int:
    """Re-tag all FLACs in a directory tree. Returns count of changed files."""
    flac_files = sorted(directory.rglob("*.flac"))
    if not flac_files:
        print(f"No FLAC files found in {directory}")
        return 0

    print(f"Found {len(flac_files)} FLAC file(s) in {directory}")
    changed = 0

    for flac in flac_files:
        tags = _read_flac_tags(flac)
        # Skip files without MB IDs
        if not tags.get("MUSICBRAINZ_ALBUMID"):
            continue

        result = retag_flac(flac, dry_run=dry_run)
        if result:
            action = "would retag" if dry_run else "retagged"
            print(f"  {action}: {flac.relative_to(directory)}")
            changed += 1

    print(f"{'Would change' if dry_run else 'Changed'} {changed}/{len(flac_files)} file(s)")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-tag FLACs with English names from MusicBrainz")
    parser.add_argument("path", help="FLAC file or directory to re-tag")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_file():
        retag_flac(target, dry_run=args.dry_run)
    elif target.is_dir():
        retag_directory(target, dry_run=args.dry_run)
    else:
        print(f"Not found: {target}")


if __name__ == "__main__":
    main()
