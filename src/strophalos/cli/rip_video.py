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
    """Rip an audio Blu-ray: handles both play-all and rip-all-dedup strategies.

    Returns the number of track files produced.
    """
    from strophalos.core.mkv import get_mkv_duration, split_by_chapters

    mb_tracks = mb_metadata.get("tracks", [])
    mb_track_count = mb_metadata.get("track_count", len(mb_tracks))
    match_method = mb_metadata.get("match_method", "")

    os.makedirs(out_dir, exist_ok=True)

    # Decide strategy based on how the MB match was made
    has_play_all = match_method == "play-all"

    if has_play_all:
        return _rip_play_all_strategy(
            drive, out_dir, durations, chapters, to_rip, mb_tracks, mb_track_count,
        )
    else:
        return _rip_all_dedup_strategy(
            drive, out_dir, durations, chapters, mb_tracks, mb_track_count,
        )


def _rip_play_all_strategy(
    drive: int,
    out_dir: str,
    durations: dict[int, int],
    chapters: dict[int, int],
    to_rip: list[int],
    mb_tracks: list[dict],
    mb_track_count: int,
) -> int:
    """Play-all strategy: rip the play-all title, split, then hunt for bonus tracks."""
    from strophalos.core.mkv import get_mkv_duration, split_by_chapters

    play_all_tid = to_rip[0]

    print(f"\n  Music BD [play-all]: ripping title {play_all_tid} "
          f"({chapters.get(play_all_tid, '?')} chapters, MB expects {mb_track_count} tracks)")

    rip_titles(drive, [play_all_tid], out_dir)

    ripped = sorted(Path(out_dir).glob("*_t*.mkv"))
    if not ripped:
        print("  Music BD: no MKV produced from play-all rip")
        return 0

    play_all_mkv = ripped[0]
    print(f"  Music BD: splitting {play_all_mkv.name} by chapters...")

    split_dir = Path(out_dir) / "_split_tmp"
    split_files = split_by_chapters(play_all_mkv, split_dir, prefix="ch")
    print(f"  Music BD: split into {len(split_files)} chapter files")

    if not split_files:
        print("  Music BD: chapter split produced no files")
        return 0

    split_durs: list[tuple[Path, float]] = []
    for f in split_files:
        dur = get_mkv_duration(f)
        split_durs.append((f, dur))

    # Rename chapter files sequentially
    for i, (f, _dur) in enumerate(split_durs):
        dest = Path(out_dir) / f"track_t{i:02d}.mkv"
        shutil.move(str(f), str(dest))

    print(f"  Music BD: {len(split_durs)} tracks from play-all")

    # Hunt for bonus tracks in non-subset titles
    play_all_ch = chapters.get(play_all_tid, 0)
    play_all_dur = durations.get(play_all_tid, 0)

    remaining_tids = []
    for tid, dur in sorted(durations.items()):
        if tid == play_all_tid:
            continue
        ch = chapters.get(tid, 0)
        if ch > 0 and dur < play_all_dur and ch < play_all_ch:
            continue
        remaining_tids.append(tid)

    if remaining_tids:
        print(f"  Music BD: {len(remaining_tids)} non-subset title(s) to check: {remaining_tids}")
        _rip_remaining_titles(drive, out_dir, remaining_tids, len(split_durs))
    elif len(split_durs) < mb_track_count:
        print(f"  Music BD: play-all has {len(split_durs)} chapters but MB expects "
              f"{mb_track_count} — no remaining titles to hunt")

    # Clean up
    play_all_mkv.unlink(missing_ok=True)
    if split_dir.exists():
        shutil.rmtree(split_dir, ignore_errors=True)

    final_files = sorted(Path(out_dir).glob("track_t*.mkv"))
    print(f"  Music BD: {len(final_files)} track files in output")
    return len(final_files)


def _rip_all_dedup_strategy(
    drive: int,
    out_dir: str,
    durations: dict[int, int],
    chapters: dict[int, int],
    mb_tracks: list[dict],
    mb_track_count: int,
) -> int:
    """Rip-all-dedup strategy: rip every title, split by chapters, deduplicate.

    Used when there's no play-all title (e.g. Shadowbringers OST with
    overlapping section playlists).
    """
    from strophalos.core.mkv import get_mkv_duration, split_by_chapters

    all_tids = sorted(durations.keys())
    print(f"\n  Music BD [rip-all]: ripping {len(all_tids)} titles, MB expects {mb_track_count} tracks")

    # Step 1: Rip all titles, split each by chapters, collect all chapter durations
    all_chapters: list[tuple[Path, float]] = []
    tmp_base = Path(out_dir) / "_rip_tmp"

    for tid in all_tids:
        ch = chapters.get(tid, 0)
        print(f"  Music BD: ripping title {tid} ({ch} chapters)...", flush=True)
        tmp_dir = tmp_base / f"t{tid}"
        rip_titles(drive, [tid], str(tmp_dir))

        ripped = sorted(tmp_dir.glob("*_t*.mkv"))
        if not ripped:
            continue

        if ch > 1:
            split_dir = tmp_dir / "_split"
            split_files = split_by_chapters(ripped[0], split_dir)
            for sf in split_files:
                dur = get_mkv_duration(sf)
                all_chapters.append((sf, dur))
            # Remove the unsplit original
            ripped[0].unlink(missing_ok=True)
        else:
            # Single chapter or unknown — keep as-is
            dur = get_mkv_duration(ripped[0])
            all_chapters.append((ripped[0], dur))

    print(f"  Music BD: {len(all_chapters)} total chapter files before dedup")

    # Step 2: Deduplicate by duration (±5s tolerance)
    unique: list[tuple[Path, float]] = []
    for path, dur in all_chapters:
        is_dup = any(abs(dur - d) <= 5.0 for _, d in unique)
        if not is_dup:
            unique.append((path, dur))

    print(f"  Music BD: {len(unique)} unique tracks after dedup (removed {len(all_chapters) - len(unique)} duplicates)")

    # Step 3: Move unique tracks to output with sequential naming
    for i, (f, dur) in enumerate(unique):
        dest = Path(out_dir) / f"track_t{i:02d}.mkv"
        shutil.move(str(f), str(dest))

    # Clean up temp dirs
    if tmp_base.exists():
        shutil.rmtree(tmp_base, ignore_errors=True)

    final_files = sorted(Path(out_dir).glob("track_t*.mkv"))
    print(f"  Music BD: {len(final_files)} track files in output")
    return len(final_files)


def _rip_remaining_titles(
    drive: int,
    out_dir: str,
    title_ids: list[int],
    start_idx: int,
) -> None:
    """Rip non-subset titles, split by chapters, and append to output.

    These are titles that aren't contained within the play-all — typically
    bonus tracks, alternate mixes, or content not on the main playlist.
    """
    from strophalos.core.mkv import get_mkv_duration, split_by_chapters

    next_idx = start_idx

    for tid in title_ids:
        print(f"  Music BD: ripping bonus title {tid}...")
        tmp_dir = Path(out_dir) / f"_bonus_t{tid}"
        rip_titles(drive, [tid], str(tmp_dir))

        ripped = sorted(tmp_dir.glob("*_t*.mkv"))
        if not ripped:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            continue

        # If title has chapters, split into individual tracks
        split_dir = tmp_dir / "_split"
        split_files = split_by_chapters(ripped[0], split_dir)

        if split_files and len(split_files) > 1:
            for sf in split_files:
                dur = get_mkv_duration(sf)
                dest = Path(out_dir) / f"track_t{next_idx:02d}.mkv"
                shutil.move(str(sf), str(dest))
                print(f"  Music BD: bonus track from title {tid} ({dur:.0f}s) → track_t{next_idx:02d}.mkv")
                next_idx += 1
        else:
            dur = get_mkv_duration(ripped[0])
            dest = Path(out_dir) / f"track_t{next_idx:02d}.mkv"
            shutil.move(str(ripped[0]), str(dest))
            print(f"  Music BD: bonus track from title {tid} ({dur:.0f}s) → track_t{next_idx:02d}.mkv")
            next_idx += 1

        shutil.rmtree(tmp_dir, ignore_errors=True)

    found = next_idx - start_idx
    print(f"  Music BD: {found} bonus track(s) from {len(title_ids)} title(s)")


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
        # Fetch full track detail from MB by release ID
        from strophalos.backends.musicbrainz import _fetch_release_detail, _get_artist, _get_release_tracks

        mb_tracks = mb_metadata.get("tracks")
        if not mb_tracks and mb_metadata.get("id"):
            release_id = mb_metadata["id"]
            tracks = _get_release_tracks({}, release_id)
            if tracks:
                mb_metadata["tracks"] = tracks
                print(f"  Music BD: fetched {len(tracks)} track details from MB release {release_id}")

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
