"""CLI entry point for rip-video — smart video disc ripper.

Scans disc, classifies content (movie/TV/music), rips selected titles,
and outputs STROPHALOS_* metadata lines for backward compatibility.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from strophalos.ripper.classify import classify_disc
from strophalos.ripper.disc_id import compute_disc_id
from strophalos.ripper.result import RipResult
from strophalos.ripper.rip import rip_titles
from strophalos.ripper.scan import (
    detect_media_type,
    get_title_chapters,
    get_title_durations,
    get_title_sizes,
    scan_disc,
)


def _rip_music_bd(
    drive: int,
    out_dir: str,
    durations: dict[int, int],
    chapters: dict[int, int],
    to_rip: list[int],
    mb_metadata: dict,
) -> int:
    """Rip an audio Blu-ray: rip play-all title, split by chapters, match to MB tracks.

    Returns the number of track files produced.
    """
    from strophalos.core.mkv import get_mkv_duration, split_by_chapters

    play_all_tid = to_rip[0]
    mb_tracks = mb_metadata.get("tracks", [])
    mb_track_count = mb_metadata.get("track_count", len(mb_tracks))

    print(f"\n  Music BD: ripping play-all title {play_all_tid} "
          f"({chapters.get(play_all_tid, '?')} chapters, MB expects {mb_track_count} tracks)")

    # Step 1: Rip the play-all title
    os.makedirs(out_dir, exist_ok=True)
    rip_titles(drive, [play_all_tid], out_dir)

    # Find the ripped MKV
    ripped = sorted(Path(out_dir).glob("*_t*.mkv"))
    if not ripped:
        print("  Music BD: no MKV produced from play-all rip")
        return 0

    play_all_mkv = ripped[0]
    print(f"  Music BD: splitting {play_all_mkv.name} by chapters...")

    # Step 2: Split by chapters into a temp dir, then rename to final pattern
    split_dir = Path(out_dir) / "_split_tmp"
    split_files = split_by_chapters(play_all_mkv, split_dir, prefix="ch")
    print(f"  Music BD: split into {len(split_files)} chapter files")

    if not split_files:
        print("  Music BD: chapter split produced no files")
        return 0

    # Step 3: Get durations of split files
    split_durs: list[tuple[Path, float]] = []
    for f in split_files:
        dur = get_mkv_duration(f)
        split_durs.append((f, dur))

    # Step 4: Match against MB tracks by duration if we have track detail
    matched_count = 0
    if mb_tracks:
        matched_count = _match_and_rename_tracks(split_durs, mb_tracks, Path(out_dir))
    else:
        # No track detail — just rename sequentially
        for i, (f, _dur) in enumerate(split_durs):
            dest = Path(out_dir) / f"track_t{i:02d}.mkv"
            shutil.move(str(f), str(dest))
            matched_count += 1

    # Step 5: Check for unmatched MB tracks and hunt in remaining titles
    if mb_tracks and matched_count < len(mb_tracks):
        unmatched_mb = _find_unmatched_mb_tracks(mb_tracks, Path(out_dir))
        if unmatched_mb:
            print(f"  Music BD: {len(unmatched_mb)} MB track(s) unmatched, "
                  f"checking remaining titles...")
            _hunt_remaining_titles(
                drive, out_dir, durations, chapters,
                play_all_tid, unmatched_mb,
            )

    # Clean up: remove play-all MKV and temp split dir
    play_all_mkv.unlink(missing_ok=True)
    if split_dir.exists():
        shutil.rmtree(split_dir, ignore_errors=True)

    final_files = sorted(Path(out_dir).glob("track_t*.mkv"))
    print(f"  Music BD: {len(final_files)} track files in output")
    return len(final_files)


def _match_and_rename_tracks(
    split_durs: list[tuple[Path, float]],
    mb_tracks: list[dict],
    out_dir: Path,
) -> int:
    """Match chapter-split files to MB tracks by sorted duration. Rename matched files."""
    from strophalos.core.fs import sanitize_filename

    # Sort both by duration
    files_by_dur = sorted(split_durs, key=lambda x: x[1])
    tracks_by_dur = sorted(enumerate(mb_tracks), key=lambda x: x[1].get("duration", 0))

    matched = 0
    # Pair up — if counts differ, zip stops at the shorter
    for (split_file, file_dur), (track_idx, track) in zip(files_by_dur, tracks_by_dur):
        track_dur = track.get("duration", 0)
        diff = abs(file_dur - track_dur)

        if diff > 10:  # more than 10s off — not a match
            print(f"  Music BD: skipping {split_file.name} ({file_dur:.0f}s) "
                  f"— too far from track {track_idx} ({track_dur:.0f}s)")
            continue

        # Rename to sequential pattern for identify-music compatibility
        dest = out_dir / f"track_t{track_idx:02d}.mkv"
        shutil.move(str(split_file), str(dest))
        matched += 1

    # Move any unmatched split files with sequential numbering
    remaining_split = sorted(out_dir.parent.rglob("_split_tmp/ch-*.mkv"))
    next_idx = len(mb_tracks)
    for f in remaining_split:
        dest = out_dir / f"track_t{next_idx:02d}.mkv"
        shutil.move(str(f), str(dest))
        next_idx += 1

    return matched


def _find_unmatched_mb_tracks(mb_tracks: list[dict], out_dir: Path) -> list[dict]:
    """Find MB tracks that don't have a corresponding file in the output dir."""
    existing = {f.name for f in out_dir.glob("track_t*.mkv")}
    unmatched = []
    for i, track in enumerate(mb_tracks):
        expected = f"track_t{i:02d}.mkv"
        if expected not in existing:
            unmatched.append(track)
    return unmatched


def _hunt_remaining_titles(
    drive: int,
    out_dir: str,
    durations: dict[int, int],
    chapters: dict[int, int],
    play_all_tid: int,
    unmatched_mb: list[dict],
) -> None:
    """Rip remaining titles and try to match their chapters against unmatched MB tracks."""
    from strophalos.core.mkv import get_mkv_duration, split_by_chapters

    unmatched_durs = [t.get("duration", 0) for t in unmatched_mb]

    for tid, title_dur in sorted(durations.items()):
        if tid == play_all_tid:
            continue
        ch_count = chapters.get(tid, 0)
        if ch_count < 1:
            continue

        # Estimate: could this title contain any of the missing tracks?
        avg_ch_dur = title_dur / ch_count if ch_count > 0 else title_dur
        possible_match = any(abs(avg_ch_dur - d) < d * 0.5 for d in unmatched_durs if d > 0)
        if not possible_match:
            continue

        print(f"  Music BD: ripping title {tid} ({ch_count} chapters) to search for missing tracks...")
        tmp_dir = Path(out_dir) / f"_hunt_t{tid}"
        rip_titles(drive, [tid], str(tmp_dir))

        ripped = sorted(tmp_dir.glob("*_t*.mkv"))
        if not ripped:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            continue

        # Split by chapters
        split_dir = tmp_dir / "_split"
        split_files = split_by_chapters(ripped[0], split_dir)

        for sf in split_files:
            sf_dur = get_mkv_duration(sf)
            # Check against each unmatched MB track
            for mb_track in list(unmatched_mb):
                mb_dur = mb_track.get("duration", 0)
                if mb_dur > 0 and abs(sf_dur - mb_dur) < 5:
                    # Found it
                    idx = unmatched_mb.index(mb_track)
                    # Find the original MB index for naming
                    # (this is the track's position in the full track list)
                    pos = mb_track.get("position", "?")
                    print(f"  Music BD: found missing track {pos} in title {tid}")
                    dest = Path(out_dir) / f"track_t{99 - len(unmatched_mb) + idx:02d}.mkv"
                    shutil.move(str(sf), str(dest))
                    unmatched_mb.remove(mb_track)
                    break

        # Clean up hunt dir
        shutil.rmtree(tmp_dir, ignore_errors=True)

        if not unmatched_mb:
            break


def rip_video_disc(
    drive: int,
    label: str | None = None,
    output: str = "/media/archive",
    output_bd_audio: str = "/output-bd",
    dry_run: bool = False,
) -> RipResult | None:
    """Core rip-video logic. Returns structured result or None on failure."""
    print(f"Scanning disc in drive {drive}...")
    disc_label, titles, disc_info = scan_disc(drive)
    print(f"Disc label: {disc_label}")
    print(f"Found {len(titles)} title(s)")

    durations = get_title_durations(titles)
    chapters = get_title_chapters(titles)
    sizes = get_title_sizes(titles)

    print("\nTitle list:")
    for tid in sorted(durations.keys()):
        dur = durations[tid]
        ch = chapters.get(tid, "?")
        sz = sizes.get(tid, 0)
        mins, secs = divmod(dur, 60)
        hours, mins = divmod(mins, 60)
        sz_gb = sz / (1024**3) if sz else 0
        size_str = f", {sz_gb:.1f} GB" if sz else ""
        print(f"  Title {tid:2d}: {hours}:{mins:02d}:{secs:02d}  ({ch} chapters{size_str})")

    disc_type, to_rip, reason, mb_metadata = classify_disc(durations, chapters, disc_label)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    media_type = detect_media_type(disc_info)
    print(f"Media type: {media_type}")

    # Compute disc ID before ripping (disc is still in drive)
    device = os.environ.get("DEVICE", "/dev/sr1")
    disc_id = compute_disc_id(media_type, device)

    if dry_run:
        print("\n[dry-run] Would rip the above titles.")
        return RipResult(
            output_dir="",
            disc_type=disc_type,
            media_type=media_type,
            title_count=len(to_rip),
            disc_id=disc_id,
            label=label or disc_label or "unknown_disc",
        )

    dir_label = label or disc_label or "unknown_disc"

    # Music BDs go to a separate output path (like audio CDs go to /output-cd)
    if disc_type == "music":
        label_dir = os.path.join(output_bd_audio, dir_label)
    else:
        content_type = {"tv": "tv"}.get(disc_type, "movies")
        label_dir = os.path.join(output, content_type, "rips", media_type, dir_label)

    # Auto-increment disc number
    disc_num = 1
    while os.path.exists(os.path.join(label_dir, f"disc{disc_num}")):
        disc_num += 1
    out_dir = os.path.join(label_dir, f"disc{disc_num}")

    # Music BD: special chapter-split rip flow
    if disc_type == "music" and mb_metadata:
        # Fetch full track detail from MB for matching
        from strophalos.backends.musicbrainz import search_release

        mb_tracks = mb_metadata.get("tracks")
        if not mb_tracks:
            # score_musicbrainz doesn't fetch full track detail — search_release does
            release = search_release(disc_label or "", list(durations.values()), prefer_bluray=True)
            if release:
                mb_metadata["tracks"] = release["tracks"]

        track_count = _rip_music_bd(drive, out_dir, durations, chapters, to_rip, mb_metadata)
    else:
        print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
        rip_titles(drive, to_rip, out_dir)
        track_count = len(to_rip)

    # Persist disc ID in output directory
    if disc_id:
        disc_meta = {
            "disc_id": disc_id,
            "media_type": media_type,
            "disc_label": dir_label,
            "disc_number": disc_num,
            "id_type": "dvd_crc64" if media_type == "dvd" else "bd_sha256",
        }
        meta_path = os.path.join(out_dir, ".disc-id.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(disc_meta, f, indent=2)
        except Exception:
            pass

    return RipResult(
        output_dir=out_dir,
        disc_type=disc_type,
        media_type=media_type,
        title_count=track_count,
        disc_id=disc_id,
        label=dir_label,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart video disc ripper")
    parser.add_argument("--drive", type=int, default=0, help="MakeMKV drive ID")
    parser.add_argument("--output", default="/media/archive", help="Archive base directory")
    parser.add_argument("--label", default=None, help="Disc volume label (DRV_LABEL) for directory naming")
    parser.add_argument("--dry-run", action="store_true", help="Scan and classify only")
    args = parser.parse_args()

    result = rip_video_disc(args.drive, args.label, args.output, args.dry_run)

    if result and not args.dry_run:
        # STROPHALOS_* output protocol — backward compatibility
        print(f"STROPHALOS_OUTPUT_DIR={result.output_dir}")
        print(f"STROPHALOS_DISC_TYPE={result.disc_type}")
        print(f"STROPHALOS_MEDIA_TYPE={result.media_type}")
        print(f"STROPHALOS_TITLE_COUNT={result.title_count}")
        if result.disc_id:
            print(f"STROPHALOS_DISC_ID={result.disc_id}")
    print("Done.")


if __name__ == "__main__":
    main()
