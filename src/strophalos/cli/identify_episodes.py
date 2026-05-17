"""CLI entry point for identify-episodes — multi-layer TV episode identification.

Pipeline:
  Phase 0: Hash-based identification (OpenSubtitles, AniDB)
  Phase 1: Duration pre-filter + subtitle acquisition
  Phase 2: Score matrix + Hungarian assignment
  Fallback: Forward-order assumption when scores are indistinct
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from strophalos.backends import anidb, opensubtitles
from strophalos.backends.opensubtitles import AuthError
from strophalos.backends.tmdb import fetch_all_episodes
from strophalos.backends.tvdb import supplement_episodes
from strophalos.core.fs import parse_season_disc, sanitize_filename
from strophalos.core.mkv import get_mkv_duration
from strophalos.core.notify import notify
from strophalos.daemon.manifest import settle_identify, write_conflict
from strophalos.identify.assignment import assign_episodes, build_double_episode_window, find_best_window
from strophalos.identify.subtitles import extract_subtitles
from strophalos.types import Episode, MatchResult, RippedFile


def _format_ranges(match_results: list[MatchResult]) -> str:
    """Build episode range strings (detect contiguous runs)."""
    eps = sorted((r.episode.season, r.episode.episode) for r in match_results)
    if not eps:
        return ""
    ranges: list[str] = []
    run_start = run_end = eps[0]
    for s, e in eps[1:]:
        prev_s, prev_e = run_end
        if s == prev_s and e == prev_e + 1:
            run_end = (s, e)
        else:
            ranges.append(_format_run(run_start, run_end))
            run_start = run_end = (s, e)
    ranges.append(_format_run(run_start, run_end))
    return ", ".join(ranges)


def _format_run(start: tuple[int, int], end: tuple[int, int]) -> str:
    if start == end:
        return f"S{start[0]:02d}E{start[1]:02d}"
    if start[0] == end[0]:
        return f"S{start[0]:02d}E{start[1]:02d}–E{end[1]:02d}"
    return f"S{start[0]:02d}E{start[1]:02d}–S{end[0]:02d}E{end[1]:02d}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Episode identification for TV disc rips")
    parser.add_argument("--dir", required=True, help="Archive disc directory containing ripped MKV files")
    parser.add_argument("--label", required=True, help="Disc label for TMDb search")
    parser.add_argument("--library", default="/media", help="Library root for hard-linked output")
    parser.add_argument("--dry-run", action="store_true", help="Show proposed links without executing")
    parser.add_argument("--min-score", type=float, default=0.5, help="Minimum score for matching")
    parser.add_argument("--verbose", action="store_true", help="Show OCR text and detailed scoring")
    parser.add_argument(
        "--search-window",
        action="store_true",
        help="Search for the best episode window instead of assuming sequential disc order",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Link even when all scores are weak (below confidence threshold)",
    )
    args = parser.parse_args()

    out_dir = Path(args.dir)
    if not out_dir.is_dir():
        print(f"Directory not found: {out_dir}")
        return

    mkv_files = sorted(out_dir.glob("*_t[0-9][0-9].mkv"))
    if not mkv_files:
        print("No MKV files found")
        return

    print(f"Found {len(mkv_files)} MKV file(s)")

    parsed = parse_season_disc(args.label)
    if parsed:
        series_prefix, label_season, label_disc = parsed
        series_name = series_prefix.replace("_", " ").strip()
        print(f"  Label: season {label_season}, disc {label_disc}")
    else:
        label_season = None
        series_name = args.label.replace("_", " ").strip()
    library_dir = Path(args.library)

    # Build RippedFile objects with durations
    files: list[RippedFile] = []
    for mkv in mkv_files:
        dur = get_mkv_duration(mkv)
        files.append(RippedFile(path=mkv, duration_seconds=dur))
        print(f"  {mkv.name}: {dur:.0f}s")

    results: dict[Path, MatchResult] = {}
    double_extra_links: list[tuple[RippedFile, Episode, MatchResult]] = []
    remaining = list(files)
    pipeline_notes: list[str] = []

    # --- Phase 0: Hash-based identification ---
    try:
        hash_results = opensubtitles.identify(mkv_files)
    except AuthError as exc:
        msg = f"Identification blocked: {exc}"
        print(f"  {msg}")
        settle_identify(
            out_dir,
            status="blocked",
            blocker="opensubtitles_token",
            summary=msg,
            notify_title=f"{series_name}: identification blocked",
            notify_body=msg,
            notify_error=True,
        )
        return
    if hash_results:
        for path, (season, episode, title) in hash_results.items():
            rf = next((f for f in remaining if f.path == path), None)
            if rf:
                ep = Episode(season=season, episode=episode, title=title, runtime_seconds=0)
                results[path] = MatchResult(file=rf, episode=ep, method="opensubtitles hash")
                remaining = [f for f in remaining if f.path != path]
        print(f"  OpenSubtitles: {len(hash_results)}/{len(files)} identified via hash")

    if remaining:
        remaining_paths = [f.path for f in remaining]
        anidb_results = anidb.identify(remaining_paths)
        if anidb_results:
            for path, (season, episode, title) in anidb_results.items():
                rf = next((f for f in remaining if f.path == path), None)
                if rf:
                    ep = Episode(season=season, episode=episode, title=title, runtime_seconds=0)
                    results[path] = MatchResult(file=rf, episode=ep, method="anidb hash")
                    remaining = [f for f in remaining if f.path != path]
            print(f"  AniDB: {len(anidb_results)}/{len(files)} identified via ed2k hash")

    if not remaining:
        print("  All files identified via hash lookup")
        pipeline_notes.append("All files identified via hash lookup")
    else:
        # --- Phase 1+: TMDb + duration + reference subtitles ---
        episodes, specials, group_name, tmdb_name, series_id = fetch_all_episodes(args.label)
        if tmdb_name:
            series_name = tmdb_name
        if not episodes and not tmdb_name:
            msg = (
                f"No TMDb match for '{series_name}'. Add the series at https://www.themoviedb.org and re-run:\n"
                f"  docker exec strophalos identify-episodes --dir {args.dir} --label {args.label}"
            )
            print(f"  {msg}")
            settle_identify(
                out_dir,
                status="no_tmdb_match",
                summary=msg,
                notify_title=f"{series_name}: identification failed",
                notify_body=msg,
                notify_error=True,
            )
            return
        elif not episodes:
            msg = f"TMDb matched '{series_name}' but no episodes found. Check the TMDb entry has seasons/episodes."
            print(f"  {msg}")
            settle_identify(
                out_dir,
                status="no_tmdb_match",
                summary=msg,
                notify_title=f"{series_name}: no episodes on TMDb",
                notify_body=msg,
                notify_error=True,
            )
            return
        if episodes:
            n_seasons = len({ep.season for ep in episodes})
            print(f"  {len(episodes)} episodes across {n_seasons} season(s)")

            # Filter to the season from the label (e.g. S1D1 → season 1)
            if label_season is not None:
                season_eps = [ep for ep in episodes if ep.season == label_season]
                if season_eps:
                    print(f"  Filtered to season {label_season}: {len(season_eps)} episode(s)")
                    episodes = season_eps
                else:
                    print(f"  Warning: no episodes for season {label_season}, using all seasons")

            # Check TVDB for a better episode list (TMDb groups can be wrong)
            if label_season is not None and tmdb_name:
                tvdb_eps = supplement_episodes(tmdb_name, label_season, episodes)
                if tvdb_eps:
                    episodes = tvdb_eps

            # Exclude already-matched and already-linked episodes
            matched_ep_keys = {(r.episode.season, r.episode.episode) for r in results.values()}
            existing_eps: set[tuple[int, int]] = set()
            lib_series_dir = library_dir / "tv" / sanitize_filename(series_name)
            if lib_series_dir.exists():
                for existing in lib_series_dir.glob("S[0-9][0-9]E[0-9][0-9]*.mkv"):
                    m = re.match(r"S(\d+)E(\d+)", existing.name)
                    if m:
                        existing_eps.add((int(m.group(1)), int(m.group(2))))
            if existing_eps:
                print(f"  {len(existing_eps)} already-linked episode(s) in library")

            exclude = matched_ep_keys | existing_eps
            episodes = [ep for ep in episodes if (ep.season, ep.episode) not in exclude]

            if episodes and remaining:
                # Duration pre-filter: check if duration alone can solve it
                need_subs = False
                for f in remaining:
                    candidates = [
                        ep
                        for ep in episodes
                        if ep.runtime_seconds <= 0
                        or abs(f.duration_seconds - ep.runtime_seconds) / max(ep.runtime_seconds, 1) <= 0.05
                    ]
                    if len(candidates) != 1:
                        need_subs = True
                        if candidates:
                            codes = ", ".join(ep.code for ep in candidates)
                            print(
                                f"  {f.path.name} ({f.duration_seconds:.0f}s): "
                                f"{len(candidates)} duration candidates — {codes}"
                            )
                            pipeline_notes.append(
                                f"Duration: ambiguous ({len(candidates)} candidates for {f.path.name})"
                            )
                        else:
                            print(f"  {f.path.name} ({f.duration_seconds:.0f}s): no duration candidates")
                            pipeline_notes.append(f"Duration: no candidates for {f.path.name}")
                        break

                # Fetch reference subs only if duration doesn't fully solve it
                reference_subs: dict[tuple[int, int], list[tuple[float, str]]] = {}
                if need_subs and series_id is not None:
                    try:
                        reference_subs = opensubtitles.fetch_reference_subs(episodes, series_id)
                    except AuthError as exc:
                        msg = f"Identification blocked: {exc}"
                        print(f"  {msg}")
                        settle_identify(
                            out_dir,
                            status="blocked",
                            blocker="opensubtitles_token",
                            summary=msg,
                            notify_title=f"{series_name}: identification blocked",
                            notify_body=msg,
                            notify_error=True,
                        )
                        return

                if need_subs and reference_subs:
                    pipeline_notes.append("Falling back to subtitle matching")
                    print("  Duration-only matching insufficient, extracting subtitles for reference comparison...")

                    def _process_file(f: RippedFile) -> tuple[RippedFile, int]:
                        f.subtitle_texts = extract_subtitles(f.path, f.duration_seconds, full=True)
                        return f, len(f.subtitle_texts)

                    with ThreadPoolExecutor(max_workers=min(4, len(remaining))) as pool:
                        futures = {pool.submit(_process_file, f): f for f in remaining}
                        for future in as_completed(futures):
                            f, n_cues = future.result()
                            print(f"  {f.path.name}: {n_cues} subtitle cue(s)")

                    # Deduplicate common cues (OP/ED)
                    if len(remaining) > 1:
                        cue_counts: Counter[str] = Counter()
                        for f in remaining:
                            seen: set[str] = set()
                            for _, text in f.subtitle_texts:
                                normalized = text.strip().lower()
                                if normalized not in seen:
                                    cue_counts[normalized] += 1
                                    seen.add(normalized)
                        threshold = len(remaining) // 2
                        common_cues = {t for t, c in cue_counts.items() if c > threshold}
                        if common_cues:
                            for f in remaining:
                                f.subtitle_texts = [
                                    (t, text) for t, text in f.subtitle_texts if text.strip().lower() not in common_cues
                                ]
                elif need_subs and not reference_subs:
                    pipeline_notes.append("Duration: ambiguous; no reference subs available — using duration only")
                elif not need_subs:
                    pipeline_notes.append("Duration matching sufficient")
                    print("  Duration matching sufficient")

                n = len(remaining)

                if args.search_window:
                    # Slide window across all candidate episodes to find best fit
                    print("  Searching for best episode window...")
                    window_start, window_score = find_best_window(remaining, episodes, reference_subs)
                    window = episodes[window_start : window_start + n]
                else:
                    # Default: next N unlinked episodes (discs ripped in order)
                    window = episodes[:n]

                # Try a double-episode-aware window as a competing candidate
                dbl_result = build_double_episode_window(remaining, episodes)
                skip_window, double_indices, skipped_map = dbl_result if dbl_result else (None, frozenset(), {})
                use_doubles = False

                if skip_window and window:
                    # Race both windows, keep the higher-scoring one
                    normal_assign = assign_episodes(remaining, window, reference_subs)
                    skip_assign = assign_episodes(remaining, skip_window, reference_subs, double_indices)
                    normal_total = sum(s for _, _, s in normal_assign)
                    skip_total = sum(s for _, _, s in skip_assign)
                    if skip_total > normal_total:
                        print(f"  Double-episode window beats sequential ({skip_total:.2f} vs {normal_total:.2f})")
                        window = skip_window
                        assignments = skip_assign
                        use_doubles = True
                    else:
                        assignments = normal_assign
                elif skip_window:
                    window = skip_window
                    assignments = None  # will be computed below
                    use_doubles = True
                else:
                    assignments = None

                if not window:
                    print("  No candidate episodes remaining")
                elif len(window) < n:
                    print(f"  Only {len(window)} episode(s) for {n} file(s) — some will be unmatched")

                if window:
                    print(f"  Window: {window[0].code}–{window[-1].code} ({len(window)} episode(s) for {n} file(s))")

                    if assignments is None:
                        assignments = assign_episodes(remaining, window, reference_subs, double_indices)
                    method = "reference subtitle match" if reference_subs else "duration"

                    # Per-file confidence gate. When subtitle matching was the
                    # active method, require real subtitle evidence (not just a
                    # duration score of ~2.0); weak files stay unmatched rather
                    # than being assigned by position. --force bypasses the gate.
                    # --min-score still applies (duration-only path).
                    SUB_CONFIDENCE_THRESHOLD = 3.0
                    sub_gate = bool(reference_subs) and not args.force

                    skipped_weak: list[tuple[str, float]] = []
                    for file_idx, ep_idx, score in assignments:
                        if score < args.min_score:
                            continue
                        if sub_gate and score < SUB_CONFIDENCE_THRESHOLD:
                            skipped_weak.append((remaining[file_idx].path.name, score))
                            continue
                        f = remaining[file_idx]
                        results[f.path] = MatchResult(
                            file=f,
                            episode=window[ep_idx],
                            method=method,
                            score=score,
                        )

                    # Elimination: when exactly one file and one window
                    # episode remain unmatched, the pairing is unambiguous.
                    claimed_ep_indices: set[int] = set()
                    for fi, ei, _score in assignments:
                        if remaining[fi].path in results:
                            claimed_ep_indices.add(ei)
                    unclaimed_files = [f for f in remaining if f.path not in results]
                    unclaimed_eps = [(i, ep) for i, ep in enumerate(window) if i not in claimed_ep_indices]
                    if len(unclaimed_files) == 1 and len(unclaimed_eps) == 1:
                        f = unclaimed_files[0]
                        _, ep = unclaimed_eps[0]
                        results[f.path] = MatchResult(
                            file=f,
                            episode=ep,
                            method="elimination",
                            score=0.0,
                        )
                        skipped_weak = [(n, s) for n, s in skipped_weak if n != f.path.name]
                        print(f"  {f.path.name} → {ep.code} [elimination — sole remaining match]")

                    if skipped_weak:
                        names = ", ".join(f"{n} ({s:.2f})" for n, s in skipped_weak)
                        print(
                            f"  Leaving {len(skipped_weak)} file(s) unmatched "
                            f"(score < {SUB_CONFIDENCE_THRESHOLD:.1f}): {names}"
                        )
                        pipeline_notes.append(
                            f"{len(skipped_weak)} file(s) unmatched below subtitle confidence threshold"
                        )

                    # Double-length files also claim the absorbed episode
                    if use_doubles and skipped_map:
                        for file_idx, skipped_ep in skipped_map.items():
                            f = remaining[file_idx]
                            if f.path in results:
                                primary = results[f.path]
                                double_extra_links.append((f, skipped_ep, primary))

    # --- Hard-link matched files to library ---
    unmatched_names: list[str] = []
    matched_manifest: dict[str, dict] = {}
    conflict_entries: list[dict] = []

    lib_series_dir = library_dir / "tv" / sanitize_filename(series_name)

    for f in files:
        if f.path not in results:
            unmatched_names.append(f.path.name)
            continue

        r = results[f.path]
        title_safe = sanitize_filename(r.episode.title)
        new_name = f"{r.episode.code} - {title_safe}.mkv" if title_safe else f"{r.episode.code}.mkv"
        link_path = lib_series_dir / new_name

        if link_path.exists():
            if link_path.stat().st_ino == f.path.stat().st_ino:
                print(f"  skip (already linked): {f.path.name} → {new_name}")
                continue
            print(f"  conflict: {new_name} exists with different inode")
            conflict_entries.append({"library_path": str(link_path), "inode": link_path.stat().st_ino})
            notify(
                f"{series_name} {r.episode.code}: link conflict",
                f"Library already has a different copy:\n{link_path}\n\nNew rip: {f.path}\nResolve manually.",
                error=True,
            )
            continue

        action = "would link" if args.dry_run else "link"
        rel_path = link_path.relative_to(library_dir) if library_dir in link_path.parents else new_name
        print(f"  {action}: {f.path.name} → {rel_path}  [{r.method}]")

        if not args.dry_run:
            lib_series_dir.mkdir(parents=True, exist_ok=True)
            os.link(f.path, link_path)

        matched_manifest[new_name] = {
            "original": str(f.path),
            "season": r.episode.season,
            "episode": r.episode.episode,
            "title": r.episode.title,
            "method": r.method,
            "score": round(r.score, 2),
        }

    # Extra links for absorbed episodes (double-length files claim two slots)
    for f, skipped_ep, primary in double_extra_links:
        title_safe = sanitize_filename(skipped_ep.title)
        new_name = f"{skipped_ep.code} - {title_safe}.mkv" if title_safe else f"{skipped_ep.code}.mkv"
        link_path = lib_series_dir / new_name

        if link_path.exists():
            if link_path.stat().st_ino == f.path.stat().st_ino:
                print(f"  skip (already linked): {f.path.name} → {new_name}")
                continue
            print(f"  conflict: {new_name} exists with different inode")
            continue

        action = "would link" if args.dry_run else "link"
        rel_path = link_path.relative_to(library_dir) if library_dir in link_path.parents else new_name
        print(f"  {action}: {f.path.name} → {rel_path}  [double-episode alias for {primary.episode.code}]")

        if not args.dry_run:
            lib_series_dir.mkdir(parents=True, exist_ok=True)
            os.link(f.path, link_path)

        matched_manifest[new_name] = {
            "original": str(f.path),
            "season": skipped_ep.season,
            "episode": skipped_ep.episode,
            "title": skipped_ep.title,
            "method": f"double-episode alias ({primary.episode.code})",
            "score": round(primary.score, 2),
        }

    if unmatched_names:
        print(f"  Unmatched: {unmatched_names}")

    # --- Write conflict marker (suppresses re-notification on next poll) ---
    if conflict_entries and not args.dry_run:
        write_conflict(out_dir, conflict_entries)

    # --- Write manifest ---
    if not args.dry_run and matched_manifest:
        manifest = {
            "series": series_name,
            "matched": matched_manifest,
            "unmatched": unmatched_names,
            "timestamp": datetime.now().isoformat(),
        }
        manifest_path = out_dir / ".episode-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  Manifest written: {manifest_path}")

    # --- Notification ---
    if not matched_manifest and not unmatched_names:
        return

    by_method: dict[str, list[MatchResult]] = {}
    for r in results.values():
        by_method.setdefault(r.method, []).append(r)

    lines: list[str] = []
    all_range = _format_ranges(list(results.values()))
    lines.append(all_range)
    lines.append("")
    for method, mrs in sorted(by_method.items()):
        r_str = _format_ranges(mrs)
        scores = sorted([r.score for r in mrs], reverse=True)
        score_str = ", ".join(f"{s:.1f}" for s in scores)
        lines.append(f"{method}: {r_str} (scores: {score_str})")
    if pipeline_notes:
        lines.append("")
        for note in pipeline_notes:
            lines.append(note)
    lines.append("")
    lines.append(f"{len(files)} title(s) → {len(matched_manifest)} linked to library")
    if unmatched_names:
        lines.append(f"{len(unmatched_names)} unmapped file(s) in archive")

    body = "\n".join(lines)
    title = f"{series_name}: episodes linked"
    print(f"\n{title}\n{body}")

    if matched_manifest and unmatched_names:
        status = "partial"
    elif matched_manifest:
        status = "success"
    else:
        status = "no_match"

    settle_identify(
        out_dir,
        status=status,
        summary=f"{len(files)} title(s) → {len(matched_manifest)} linked",
        notify_title=title,
        notify_body=body,
        notify_error=bool(unmatched_names),
    )


if __name__ == "__main__":
    main()
