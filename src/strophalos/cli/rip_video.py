"""CLI entry point for rip-video — smart video disc ripper.

Two-pass flow: `plan_video_disc` scans/classifies and writes the rip plan
(first insertion); `rip_planned_video_disc` rips per an existing plan
(re-insertion).  `rip_video_disc` dispatches between them by disc_id and
also offers the legacy one-insertion flow via `single_pass=True`.

Outputs STROPHALOS_* metadata lines for backward compatibility.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from strophalos.ripper.classify import classify_disc
from strophalos.ripper.dedup import dedup_segment_variants, filter_bitrate_outliers
from strophalos.ripper.disc_id import compute_disc_id
from strophalos.ripper.orchestrate import run_rip, validate_rip
from strophalos.ripper.plan import (
    ClassificationRecord,
    PlanBlock,
    PlanValidationError,
    RipPlan,
    build_title_records,
    find_plan_by_disc_id,
    locate_plan,
    pinned_release_id,
    plan_path,
    validate_plan,
)
from strophalos.ripper.planner import (
    compute_early_disc_id,
    resolve_output_dir,
    seed_or_refresh_plan,
    wipe_rip_dir,
)
from strophalos.ripper.result import RipResult
from strophalos.ripper.rip import rip_titles
from strophalos.ripper.scan import (
    detect_media_type,
    get_title_chapters,
    get_title_durations,
    get_title_segment_maps,
    get_title_sizes,
    get_title_source_filenames,
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
    mb_tracks = mb_metadata.get("tracks", [])
    mb_track_count = mb_metadata.get("track_count", len(mb_tracks))
    match_method = mb_metadata.get("match_method", "")

    os.makedirs(out_dir, exist_ok=True)

    # Decide strategy based on how the MB match was made
    has_play_all = match_method == "play-all"

    if has_play_all:
        return _rip_play_all_strategy(
            drive,
            out_dir,
            durations,
            chapters,
            to_rip,
            mb_tracks,
            mb_track_count,
        )
    else:
        return _rip_all_dedup_strategy(
            drive,
            out_dir,
            durations,
            chapters,
            mb_tracks,
            mb_track_count,
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

    print(
        f"\n  Music BD [play-all]: ripping title {play_all_tid} "
        f"({chapters.get(play_all_tid, '?')} chapters, MB expects {mb_track_count} tracks)"
    )

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
        print(
            f"  Music BD: play-all has {len(split_durs)} chapters but MB expects "
            f"{mb_track_count} — no remaining titles to hunt"
        )

    # Clean up
    play_all_mkv.unlink(missing_ok=True)
    if split_dir.exists():
        shutil.rmtree(split_dir, ignore_errors=True)

    final_files = sorted(Path(out_dir).glob("track_t*.mkv"))
    print(f"  Music BD: {len(final_files)} track files in output")
    return len(final_files)


def _slide_window_match(
    chapter_durs: list[float],
    mb_durs: list[float],
    tolerance: float = 5.0,
) -> tuple[int, float]:
    """Slide a title's chapter durations across MB track list, find best fit.

    Returns (best_position, avg_diff_at_best_position).
    Position is the MB track index where this title's first chapter aligns.
    """
    n_ch = len(chapter_durs)
    n_mb = len(mb_durs)

    if n_ch == 0 or n_mb == 0:
        return 0, float("inf")

    best_pos = 0
    best_avg = float("inf")

    for pos in range(n_mb - n_ch + 1):
        total_diff = sum(abs(chapter_durs[i] - mb_durs[pos + i]) for i in range(n_ch))
        avg = total_diff / n_ch
        if avg < best_avg:
            best_avg = avg
            best_pos = pos

    return best_pos, best_avg


def _rip_all_dedup_strategy(
    drive: int,
    out_dir: str,
    durations: dict[int, int],
    chapters: dict[int, int],
    mb_tracks: list[dict],
    mb_track_count: int,
) -> int:
    """Rip-all + sliding window strategy: rip every title, split by chapters,
    then match each title's chapter sequence against MB track positions.

    Each title's chapters are a contiguous run within MB's track listing.
    We slide each title's duration sequence across MB to find its position,
    then keep the best source file for each MB track position.

    Used when there's no play-all title (e.g. Shadowbringers OST).
    """
    from strophalos.core.mkv import get_mkv_duration, split_by_chapters

    all_tids = sorted(durations.keys())
    print(f"\n  Music BD [rip-all]: ripping {len(all_tids)} titles, MB expects {mb_track_count} tracks")

    mb_durs = [t.get("duration", 0) for t in mb_tracks]

    # Step 1: Rip all titles, split by chapters, collect per-title chapter sequences
    # Each entry: (title_id, [(file_path, duration), ...])
    title_chapters: list[tuple[int, list[tuple[Path, float]]]] = []
    tmp_base = Path(out_dir) / "_rip_tmp"

    for idx, tid in enumerate(all_tids):
        ch = chapters.get(tid, 0)
        print(f"  Music BD: ripping title {tid} ({idx + 1}/{len(all_tids)}, {ch} chapters)...", flush=True)
        tmp_dir = tmp_base / f"t{tid}"
        rip_titles(drive, [tid], str(tmp_dir))

        ripped = sorted(tmp_dir.glob("*_t*.mkv"))
        if not ripped:
            continue

        if ch > 1:
            split_dir = tmp_dir / "_split"
            split_files = split_by_chapters(ripped[0], split_dir)
            ch_files = []
            for sf in split_files:
                dur = get_mkv_duration(sf)
                ch_files.append((sf, dur))
            title_chapters.append((tid, ch_files))
            ripped[0].unlink(missing_ok=True)
        else:
            dur = get_mkv_duration(ripped[0])
            title_chapters.append((tid, [(ripped[0], dur)]))

    total_files = sum(len(chs) for _, chs in title_chapters)
    print(f"  Music BD: {total_files} total chapter files from {len(title_chapters)} titles")

    # Step 2: Slide each title's chapter sequence against MB track list
    # For each MB track position, track the best (lowest diff) source file
    # best_source[mb_pos] = (file_path, diff)
    best_source: dict[int, tuple[Path, float]] = {}

    for tid, ch_files in title_chapters:
        ch_durs = [d for _, d in ch_files]

        if not mb_durs or all(d == 0 for d in mb_durs):
            # MB has no duration data — can't window-match, assign sequentially
            continue

        # For zero-duration MB tracks, skip window matching for this title
        # if the MB durations at the candidate positions are all 0
        pos, avg_diff = _slide_window_match(ch_durs, mb_durs)

        if avg_diff > 30:
            # No good fit — might be bonus content not in MB listing
            print(
                f"  Music BD: title {tid} ({len(ch_files)} ch) — no good window match "
                f"(best avg diff {avg_diff:.0f}s at pos {pos})"
            )
            continue

        print(
            f"  Music BD: title {tid} ({len(ch_files)} ch) → MB tracks {pos}-{pos + len(ch_files) - 1} "
            f"(avg diff {avg_diff:.1f}s)"
        )

        for i, (f, dur) in enumerate(ch_files):
            mb_pos = pos + i
            diff = abs(dur - mb_durs[mb_pos]) if mb_pos < len(mb_durs) else float("inf")
            existing = best_source.get(mb_pos)
            if existing is None or diff < existing[1]:
                best_source[mb_pos] = (f, diff)

    # Step 3: Collect unmatched files (not assigned to any MB position)
    unmatched_files: list[tuple[Path, float]] = []
    matched_paths = {str(p) for p, _ in best_source.values()}
    for _tid, ch_files in title_chapters:
        for f, dur in ch_files:
            if str(f) not in matched_paths:
                unmatched_files.append((f, dur))

    # Step 4: Fill remaining MB gaps by duration matching against unmatched pool.
    # For each unfilled MB position, find the closest-duration unmatched file.
    unfilled = [pos for pos in range(mb_track_count) if pos not in best_source]
    if unfilled and unmatched_files:
        print(f"  Music BD: filling {len(unfilled)} remaining MB gaps by duration...")
        used_paths: set[str] = set()
        for mb_pos in unfilled:
            target_dur = mb_durs[mb_pos]
            # Find closest unmatched file by duration
            best_f = None
            best_d = float("inf")
            for f, dur in unmatched_files:
                if str(f) in used_paths:
                    continue
                d = abs(dur - target_dur)
                if d < best_d:
                    best_d = d
                    best_f = f
            if best_f is not None:
                best_source[mb_pos] = (best_f, best_d)
                used_paths.add(str(best_f))
                print(f"  Music BD: gap fill MB pos {mb_pos} ({target_dur:.0f}s) ← {best_f.name} (diff {best_d:.1f}s)")

        # Update unmatched list
        unmatched_files = [(f, d) for f, d in unmatched_files if str(f) not in used_paths]

    # Deduplicate remaining unmatched files against matched (±5s)
    truly_unmatched: list[tuple[Path, float]] = []
    for f, dur in unmatched_files:
        is_dup = any(abs(dur - d) <= 5.0 for _, (_, d) in best_source.items())
        if not is_dup:
            truly_unmatched.append((f, dur))

    # Step 5: Move best source files to output
    os.makedirs(out_dir, exist_ok=True)
    for mb_pos in sorted(best_source.keys()):
        f, _diff = best_source[mb_pos]
        dest = Path(out_dir) / f"track_t{mb_pos:02d}.mkv"
        shutil.move(str(f), str(dest))

    # Append truly unique bonus tracks after the MB positions
    next_idx = mb_track_count
    for f, dur in truly_unmatched:
        dest = Path(out_dir) / f"track_t{next_idx:02d}.mkv"
        shutil.move(str(f), str(dest))
        print(f"  Music BD: bonus track ({dur:.0f}s) → track_t{next_idx:02d}.mkv")
        next_idx += 1

    # Clean up
    if tmp_base.exists():
        shutil.rmtree(tmp_base, ignore_errors=True)

    matched = len(best_source)
    bonus = next_idx - mb_track_count
    print(f"  Music BD: {matched}/{mb_track_count} MB tracks matched, {bonus} bonus track(s)")

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


@dataclass
class VideoScan:
    """Result of the slow makemkvcon scan phase."""

    disc_label: str | None
    all_titles: dict  # every scanned title (tid → attr map)
    titles: dict  # post dedup / bitrate-filter
    durations: dict[int, int]  # filtered
    chapters: dict[int, int]  # filtered
    media_type: str
    dropped_reasons: dict[int, str] = field(default_factory=dict)


def scan_video_disc(drive: int) -> VideoScan:
    """Scan via makemkvcon, dedup segment variants, filter bitrate outliers."""
    print(f"Scanning disc in drive {drive}...")
    disc_label, all_titles, disc_info = scan_disc(drive)
    print(f"Disc label: {disc_label}")
    print(f"Found {len(all_titles)} title(s)")

    dropped_reasons: dict[int, str] = {}

    titles, dropped = dedup_segment_variants(all_titles)
    for dropped_tid, kept_tid in dropped:
        print(f"  dedup: dropping title {dropped_tid} (segment duplicate of title {kept_tid})")
        dropped_reasons[dropped_tid] = f"segment-duplicate-of-{kept_tid}"

    titles, low_bitrate = filter_bitrate_outliers(titles)
    for dropped_tid, br_mbps, median_mbps in low_bitrate:
        print(
            f"  bitrate: dropping title {dropped_tid} "
            f"({br_mbps:.1f} Mbps vs {median_mbps:.1f} Mbps median — likely extra/recap)"
        )
        dropped_reasons[dropped_tid] = f"low-bitrate-{br_mbps:.1f}Mbps-vs-{median_mbps:.1f}Mbps"

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

    media_type = detect_media_type(disc_info)
    print(f"Media type: {media_type}")

    return VideoScan(
        disc_label=disc_label,
        all_titles=all_titles,
        titles=titles,
        durations=durations,
        chapters=chapters,
        media_type=media_type,
        dropped_reasons=dropped_reasons,
    )


@dataclass
class _Classified:
    """Classifier verdict for one scan, plus the pristine pre-override snapshot."""

    disc_type: str
    to_rip: list[int]
    reason: str
    mb_metadata: dict | None
    classifier_disc_type: str
    classifier_suggested: list[int]


def _classify_scan(scan: VideoScan) -> _Classified:
    disc_type, to_rip, reason, mb_metadata = classify_disc(scan.durations, scan.chapters, scan.disc_label)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    # Preserve the classifier's verdict before any env/override mutation — the
    # plan's classification block must report what the classifier actually
    # decided so ID-side consumers can recover the main title even when we
    # rip all titles for extras.
    classifier_disc_type = disc_type
    classifier_suggested = list(to_rip)

    # STROPHALOS_RIP_ALL_TITLES=1 — force ripping every scanned title and
    # bypass the music-BD chapter-split flow.  Only affects first-seed of a
    # plan; once a plan file exists for the disc, its plan block wins.
    rip_all_env = os.environ.get("STROPHALOS_RIP_ALL_TITLES", "").strip().lower() in ("1", "true", "yes")
    if rip_all_env:
        all_tids = sorted(scan.durations.keys())
        print(f"\nSTROPHALOS_RIP_ALL_TITLES=1: overriding to {len(all_tids)} title(s): {all_tids}")
        to_rip = all_tids
        # Keep disc_type so the output landing path stays sensible (music
        # BDs still go to output_bd_audio), but clear mb_metadata so the
        # music-BD chapter-split flow is bypassed and titles rip raw.
        mb_metadata = None

    return _Classified(
        disc_type=disc_type,
        to_rip=list(to_rip),
        reason=reason,
        mb_metadata=mb_metadata,
        classifier_disc_type=classifier_disc_type,
        classifier_suggested=classifier_suggested,
    )


def _write_plan_for_scan(
    scan: VideoScan,
    cls: _Classified,
    disc_id: str,
    out_dir: str,
    existing_plan: RipPlan | None,
) -> RipPlan:
    return seed_or_refresh_plan(
        out_dir=out_dir,
        disc_id=disc_id,
        id_type="dvd_crc64" if scan.media_type == "dvd" else "bd_sha256",
        disc_label=scan.disc_label,
        media_type=scan.media_type,
        title_records=build_title_records(
            durations=get_title_durations(scan.all_titles),
            chapters=get_title_chapters(scan.all_titles),
            sizes=get_title_sizes(scan.all_titles),
            source_filenames=get_title_source_filenames(scan.all_titles),
            segment_maps=get_title_segment_maps(scan.all_titles),
            dropped=scan.dropped_reasons,
        ),
        classification=ClassificationRecord(
            disc_type=cls.classifier_disc_type,
            reason=cls.reason,
            suggested_titles_to_rip=cls.classifier_suggested,
        ),
        existing=existing_plan,
        default_block=PlanBlock(disc_type=cls.disc_type, titles_to_rip=list(cls.to_rip)),
    )


def _pinned_mb_metadata(plan: RipPlan, fallback: dict | None) -> dict | None:
    """mb_metadata for the music path: plan's pinned MB release wins over
    whatever the classifier's own MusicBrainz search turned up."""
    release_id = pinned_release_id(plan.identify)
    if release_id:
        print(f"  Music BD: using MusicBrainz release pinned in plan: {release_id}")
        return {"id": release_id}
    return fallback


def _execute_rip(
    drive: int,
    scan: VideoScan,
    out_dir: str,
    disc_type: str,
    to_rip: list[int],
    mb_metadata: dict | None,
    wipe_existing: bool,
) -> int | None:
    """Run the actual rip (music chapter-split or plain titles) into out_dir.

    Returns the produced track/title count, or None on failure.
    """
    # Re-rip into an existing dir: wipe stale MKVs and identify artifacts so
    # neither old titles nor old identification linger.  Plan + disc-id are
    # preserved.
    if wipe_existing:
        wipe_rip_dir(out_dir)

    # Music BD: special chapter-split rip flow
    if disc_type == "music" and mb_metadata:
        # Fetch full track detail from MB by release ID
        from strophalos.backends.musicbrainz import _get_release_tracks

        mb_tracks = mb_metadata.get("tracks")
        if not mb_tracks and mb_metadata.get("id"):
            release_id = mb_metadata["id"]
            tracks = _get_release_tracks({}, release_id)
            if tracks:
                mb_metadata["tracks"] = tracks
                print(f"  Music BD: fetched {len(tracks)} track details from MB release {release_id}")

        return _rip_music_bd(drive, out_dir, scan.durations, scan.chapters, to_rip, mb_metadata)

    print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
    if not run_rip(lambda: rip_titles(drive, to_rip, out_dir), out_dir):
        return None
    return validate_rip(out_dir)


def _write_disc_id_meta(out_dir: str, disc_id: str | None, media_type: str, dir_label: str, disc_num: int) -> None:
    """Persist .disc-id.json in the output directory."""
    if not disc_id:
        return
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


def plan_video_disc(
    drive: int,
    label: str | None = None,
    output: str = "/media/archive",
    output_bd_audio: str = "/output-bd",
    disc_id: str | None = None,
) -> RipResult | None:
    """Pass 1: scan, classify, and write the rip plan.  No rip.

    An existing plan for the disc is refreshed in place (user's plan/identify
    blocks preserved); otherwise a fresh plan is seeded into the prospective
    output dir.  Returns a `planned=True` RipResult, or None when the disc
    can't be fingerprinted (no plan written — caller falls back single-pass).
    """
    scan = scan_video_disc(drive)
    cls = _classify_scan(scan)

    if disc_id is None:
        device = os.environ.get("DEVICE", "/dev/sr1")
        disc_id = compute_disc_id(scan.media_type, device)
    if not disc_id:
        print("  Plan: skipped (no disc_id)")
        return None

    dir_label = label or scan.disc_label or "unknown_disc"

    # Music BDs land under output_bd_audio, not the main archive, so search
    # both roots — otherwise an edited plan for a music disc gets missed and
    # the re-plan lands in disc2 with the classifier's original pick.
    existing = find_plan_by_disc_id([output, output_bd_audio], disc_id)
    if existing:
        out_dir = str(existing[0])
        existing_plan = existing[1]
        print(f"\nRefreshing existing plan at {out_dir} (plan/identify blocks preserved)")
    else:
        existing_plan = None
        out_dir, _ = resolve_output_dir(
            disc_type=cls.disc_type,
            media_type=scan.media_type,
            dir_label=dir_label,
            disc_label=scan.disc_label,
            label=label,
            output=output,
            output_bd_audio=output_bd_audio,
        )

    plan = _write_plan_for_scan(scan, cls, disc_id, out_dir, existing_plan)
    print(
        f"\nPlanned {len(plan.plan.titles_to_rip)} title(s).  Edit {plan_path(out_dir)} then re-insert to rip:\n"
        "    - plan.titles_to_rip / plan.disc_type — drives the rip.\n"
        "    - identify.url — whole-disc TMDb/MusicBrainz pin.\n"
        "    - identify.matches — per-title pins (multi-movie discs): "
        '{"url": "https://www.themoviedb.org/movie/...", "titles": [N]}.\n'
        "    - rm the file to reset to classifier defaults."
    )
    return RipResult(
        output_dir=out_dir,
        disc_type=plan.plan.disc_type,
        media_type=scan.media_type,
        title_count=len(plan.plan.titles_to_rip),
        disc_id=disc_id,
        label=dir_label,
        planned=True,
    )


def rip_planned_video_disc(
    drive: int,
    plan_dir: str,
    plan: RipPlan,
    label: str | None = None,
) -> RipResult | None:
    """Pass 2: rip a re-inserted disc per its existing plan.

    Scans (the rip needs the drive mapped anyway), validates the plan
    against the scanned titles BEFORE wiping anything, refreshes the plan's
    informational blocks on disk, then rips into the plan's directory.

    Raises PlanValidationError when the plan no longer matches the disc.
    """
    scan = scan_video_disc(drive)
    cls = _classify_scan(scan)

    validate_plan(plan, scanned_tids=set(scan.all_titles))

    out_dir = str(plan_dir)
    disc_type = plan.plan.disc_type
    to_rip = list(plan.plan.titles_to_rip)
    dir_label = label or scan.disc_label or "unknown_disc"

    tag = " [user-edited]" if to_rip != list(plan.classification.suggested_titles_to_rip) else ""
    print(f"\nRipping per plan: {to_rip} (disc_type={disc_type})  [from {out_dir}]{tag}")

    plan = _write_plan_for_scan(scan, cls, plan.disc_id, out_dir, plan)

    mb_metadata = cls.mb_metadata
    if disc_type == "music":
        mb_metadata = _pinned_mb_metadata(plan, mb_metadata)

    track_count = _execute_rip(drive, scan, out_dir, disc_type, to_rip, mb_metadata, wipe_existing=True)
    if track_count is None:
        return None

    _write_disc_id_meta(out_dir, plan.disc_id, scan.media_type, dir_label, disc_num=1)

    return RipResult(
        output_dir=out_dir,
        disc_type=disc_type,
        media_type=scan.media_type,
        title_count=track_count,
        disc_id=plan.disc_id,
        label=dir_label,
        mb_metadata=mb_metadata,
    )


def _rip_single_pass(
    drive: int,
    label: str | None,
    output: str,
    output_bd_audio: str,
    disc_id: str | None = None,
) -> RipResult | None:
    """Legacy one-insertion flow: scan, plan, and rip immediately.

    Still honors an existing plan (same dir, user's titles/disc_type), and
    still writes/refreshes the plan file — it just doesn't stop for review.
    Also the fallback when no disc_id can be computed (plan skipped).
    """
    scan = scan_video_disc(drive)
    cls = _classify_scan(scan)

    if disc_id is None:
        device = os.environ.get("DEVICE", "/dev/sr1")
        disc_id = compute_disc_id(scan.media_type, device)

    disc_type, to_rip, mb_metadata = cls.disc_type, list(cls.to_rip), cls.mb_metadata
    dir_label = label or scan.disc_label or "unknown_disc"

    existing = find_plan_by_disc_id([output, output_bd_audio], disc_id) if disc_id else None
    if existing:
        out_dir = str(existing[0])
        disc_num = 1
        prev_plan = existing[1]
        disc_type = prev_plan.plan.disc_type
        to_rip = list(prev_plan.plan.titles_to_rip)
        tag = " [user-edited]" if to_rip != list(prev_plan.classification.suggested_titles_to_rip) else ""
        print(f"\nUsing existing plan: {to_rip} (disc_type={disc_type})  [from {out_dir}]{tag}")
    else:
        prev_plan = None
        out_dir, disc_num = resolve_output_dir(
            disc_type=disc_type,
            media_type=scan.media_type,
            dir_label=dir_label,
            disc_label=scan.disc_label,
            label=label,
            output=output,
            output_bd_audio=output_bd_audio,
        )

    if disc_id:
        plan = _write_plan_for_scan(scan, cls, disc_id, out_dir, prev_plan)
        if disc_type == "music":
            mb_metadata = _pinned_mb_metadata(plan, mb_metadata)
    else:
        print("  Plan: skipped (no disc_id)")

    track_count = _execute_rip(drive, scan, out_dir, disc_type, to_rip, mb_metadata, wipe_existing=existing is not None)
    if track_count is None:
        return None

    _write_disc_id_meta(out_dir, disc_id, scan.media_type, dir_label, disc_num)

    return RipResult(
        output_dir=out_dir,
        disc_type=disc_type,
        media_type=scan.media_type,
        title_count=track_count,
        disc_id=disc_id,
        label=dir_label,
        mb_metadata=mb_metadata,
    )


def rip_video_disc(
    drive: int,
    label: str | None = None,
    output: str = "/media/archive",
    output_bd_audio: str = "/output-bd",
    single_pass: bool = False,
) -> RipResult | None:
    """One-call dispatch (rip-video CLI and single-pass fallback).

    Two-pass by default: no plan for this disc → write one and return a
    `planned=True` result (caller ejects for review); plan exists → validate
    and rip per plan.  `single_pass=True` restores the legacy
    scan-and-rip-in-one-insertion behavior; discs that can't be
    fingerprinted fall back to it automatically.

    Raises PlanValidationError when an existing plan fails validation.
    """
    device = os.environ.get("DEVICE", "/dev/sr1")
    disc_id, _id_type = compute_early_disc_id("unknown", device)

    if single_pass or not disc_id:
        if not disc_id:
            print("no disc_id (mount failed?) — falling back to single-pass rip")
        return _rip_single_pass(drive, label, output, output_bd_audio, disc_id)

    hit = locate_plan([output, output_bd_audio], disc_id)
    if hit is not None and hit.plan is None:
        raise PlanValidationError(f"{hit.error}\nFix the JSON (or rm it) and retry.")
    if hit is None:
        return plan_video_disc(drive, label, output, output_bd_audio, disc_id=disc_id)

    validate_plan(hit.plan)
    return rip_planned_video_disc(drive, str(hit.rip_dir), hit.plan, label=label)


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart video disc ripper")
    parser.add_argument("--drive", type=int, default=0, help="MakeMKV drive ID")
    parser.add_argument("--output", default="/media/archive", help="Archive base directory")
    parser.add_argument("--label", default=None, help="Disc volume label (DRV_LABEL) for directory naming")
    parser.add_argument(
        "--plan-only",
        "--dry-run",
        action="store_true",
        dest="plan_only",
        help="Scan/classify and write the rip plan only (no rip)",
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="Legacy one-insertion flow: scan and rip immediately (no plan review stop)",
    )
    args = parser.parse_args()

    if args.plan_only:
        result = plan_video_disc(args.drive, args.label, args.output)
    else:
        try:
            result = rip_video_disc(args.drive, args.label, args.output, single_pass=args.single_pass)
        except PlanValidationError as e:
            print(f"rip plan invalid:\n{e}")
            result = None

    if result and not result.planned and not args.plan_only:
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
