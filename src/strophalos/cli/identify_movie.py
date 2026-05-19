"""CLI entry point for identify-movie — TMDb lookup + hard-link to library."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from strophalos.backends.tmdb import clean_movie_label, fetch_movie_by_id, search_movie
from strophalos.core.fs import sanitize_filename
from strophalos.core.mkv import get_mkv_duration
from strophalos.core.notify import notify
from strophalos.daemon.manifest import settle_identify, write_conflict
from strophalos.ripper.plan import read_plan

# Tokens that carry no identifying signal — they show up in catalog codes,
# generic placeholder titles ("title_t00.mkv" → "title"), and edition
# suffixes, and would otherwise produce false token-overlap matches.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "of",
    "in",
    "at",
    "to",
    "on",
    "for",
    "with",
    "disc",
    "disk",
    "title",
    "untitled",
    "bd",
    "bdmv",
    "dvd",
    "uhd",
    "hd",
    "edition",
    "extended",
    "complete",
    "collection",
    "vol",
    "volume",
}


def _tokens(s: str | None) -> set[str]:
    """Lowercase alphanumeric tokens (>=2 chars, not stopwords)."""
    if not s:
        return set()
    return {t for t in re.findall(r"[A-Za-z0-9]+", s.lower()) if len(t) >= 2 and t not in _STOPWORDS}


def _resolved_title_plausible(label: str, mkv_stem: str, winning_query: str, resolved_title: str) -> bool:
    """True if the resolved TMDb title shares a meaningful token with the
    query that produced it (or with the raw disc label / MKV stem).

    Catalog-code labels like ``HALO_22`` survive TMDb's runtime-closeness
    pick without any string relationship to the picked movie; this guard
    is what stops a 92-minute concert disc from getting silently filed as
    a coincidentally-90-minute boxing flick.
    """
    result = _tokens(resolved_title)
    if not result:
        return True  # Nothing to compare against — don't block on this alone
    sources = _tokens(label) | _tokens(mkv_stem) | _tokens(winning_query)
    return bool(result & sources)


def _link_as_unidentified(
    disc_dir: Path,
    library: Path,
    label: str,
    main_feature: Path,
    extras: list[Path],
    *,
    resolved_title: str | None,
    winning_query: str | None,
    dry_run: bool,
) -> None:
    """Hard-link the rip to ``library/movies/<label>/`` with original names.

    Used when TMDb returns a result whose title has no token overlap with
    the search input — i.e. a likely false-positive match.  The user gets
    something they can browse and rename rather than a confidently-wrong
    folder, and the state machine still records ``no_tmdb_match`` so a
    later retry (after they fix the TMDb entry or edit the plan) can take
    over.
    """
    label_safe = sanitize_filename(label.replace("_", " ").strip()) or "Unidentified"
    movie_dir = library / "movies" / label_safe
    action = "would link" if dry_run else "link"
    print(f"  {action} as unidentified: movies/{label_safe}/")
    if not dry_run:
        movie_dir.mkdir(parents=True, exist_ok=True)
    for src in (main_feature, *extras):
        dest = movie_dir / src.name
        if dest.exists():
            if dest.stat().st_ino == src.stat().st_ino:
                continue  # Already linked
            print(f"  skip (conflict): {dest}")
            continue
        print(f"  {action}: {src.name} → movies/{label_safe}/{src.name}")
        if not dry_run:
            os.link(src, dest)

    msg = (
        f"TMDb returned '{resolved_title}' for query '{winning_query}' but it shares no "
        f"meaningful tokens with label '{label}'. Linked to movies/{label_safe}/ for "
        "manual review. Fix the TMDb entry or pin identify.tmdb_id in .rip-plan.json "
        "and re-insert the disc to retry."
    )
    print(f"  {msg}")
    settle_identify(
        disc_dir,
        status="no_tmdb_match",
        summary=msg,
        notify_title=f"{label}: identification suspect — linked unidentified",
        notify_body=msg,
        notify_error=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Movie identification — hard-link to library")
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

    # Plan-aware main-feature selection: when the disc was ripped with
    # STROPHALOS_RIP_ALL_TITLES (or a manual override that pulled in extras),
    # the largest file isn't reliably the main feature.  The classifier's
    # own main-title pick lives in the rip plan — prefer that, fall back to
    # largest when no plan exists or the suggested title isn't on disk.
    plan = read_plan(disc_dir)
    main_feature: Path | None = None
    plan_note = ""
    if plan is not None:
        cls = plan.classification
        if cls.disc_type == "movie" and len(cls.suggested_titles_to_rip) == 1:
            main_tid = cls.suggested_titles_to_rip[0]
            suffix = f"_t{main_tid:02d}.mkv"
            for f in mkv_files:
                if f.name.endswith(suffix):
                    main_feature = f
                    plan_note = f" [plan: classifier main title {main_tid}]"
                    break
            if main_feature is None:
                print(
                    f"  Plan suggests title {main_tid} as main feature but no "
                    f"matching MKV (*_t{main_tid:02d}.mkv) on disk; falling back to largest"
                )
        elif cls.disc_type != "movie":
            # Classifier disagreed with the rip target (e.g. TV/music ripped
            # via plan override).  Note it but keep going — caller invoked
            # identify-movie deliberately.
            plan_note = f" [plan classifier said: {cls.disc_type}]"
    if main_feature is None:
        main_feature = max(mkv_files, key=lambda p: p.stat().st_size)

    main_size_gb = main_feature.stat().st_size / (1024**3)
    extras = [f for f in mkv_files if f != main_feature]

    print(f"  Main feature: {main_feature.name} ({main_size_gb:.1f} GB){plan_note}")
    if extras:
        print(f"  Extras: {len(extras)} file(s)")

    # Search TMDb — pass main feature duration so ambiguous labels can be
    # disambiguated by runtime (e.g. 25-min OVA vs 101-min film).
    duration = get_mkv_duration(main_feature)
    if duration:
        print(f"  Duration: {duration / 60:.0f}m")
    movie: dict | None = None
    winning_query: str | None = None
    pinned = False
    if plan is not None and plan.identify.tmdb_id and plan.identify.tmdb_type == "movie":
        print(f"  TMDb: using pinned override id={plan.identify.tmdb_id} from .rip-plan.json")
        movie = fetch_movie_by_id(plan.identify.tmdb_id)
        if movie:
            pinned = True
        else:
            print(f"  TMDb override id={plan.identify.tmdb_id} did not resolve; falling back to search")

    if movie is None:
        match = search_movie(args.label, duration_seconds=duration, mkv_title=main_feature.stem)
        if not match:
            clean = clean_movie_label(args.label)
            msg = f"No TMDb match for '{clean}'. Add the movie at https://www.themoviedb.org and re-run."
            print(f"  {msg}")
            settle_identify(
                disc_dir,
                status="no_tmdb_match",
                summary=msg,
                notify_title=f"{clean}: identification failed",
                notify_body=msg,
                notify_error=True,
            )
            return
        movie, winning_query = match

    title = movie.get("title", clean_movie_label(args.label))
    year = movie.get("release_date", "")[:4]

    # Sanity-check the resolved title against the search input.  When the
    # disc label is a pure catalog code (HALO_22, UPK75 etc.) and Kagi can't
    # resolve it, /search/movie's top-5 runtime-closest pick is essentially
    # random; without this gate we'd silently file the rip under that wrong
    # title.  See _resolved_title_plausible for the overlap rule.  Pinned
    # plan overrides bypass this — the user explicitly chose the ID.
    if not pinned and not _resolved_title_plausible(args.label, main_feature.stem, winning_query or "", title):
        print(
            f"  TMDb match '{title}' ({year}) has no token overlap with query '{winning_query}' / label '{args.label}'"
        )
        _link_as_unidentified(
            disc_dir,
            library,
            args.label,
            main_feature,
            extras,
            resolved_title=f"{title} ({year})" if year else title,
            winning_query=winning_query,
            dry_run=args.dry_run,
        )
        return
    title_safe = sanitize_filename(title)
    folder_name = f"{title_safe} ({year})" if year else title_safe

    # Library path
    movie_dir = library / "movies" / folder_name
    link_name = f"{title_safe}.mkv"
    link_path = movie_dir / link_name

    if link_path.exists():
        existing_inode = link_path.stat().st_ino
        new_inode = main_feature.stat().st_ino
        if existing_inode == new_inode:
            print(f"  Already linked (same file): {link_path}")
            return

        # Check if this is an alternate cut (different duration → extended/theatrical)
        existing_duration = get_mkv_duration(link_path)
        new_duration = duration or get_mkv_duration(main_feature)
        duration_diff_min = abs(new_duration - existing_duration) / 60 if (existing_duration and new_duration) else 0

        if duration_diff_min > 8:
            # Durations differ enough to be a different cut — suffix the longer one
            if new_duration > existing_duration:
                suffix = "Extended Cut"
            else:
                suffix = "Alternate Cut"
            link_name = f"{title_safe} - {suffix}.mkv"
            link_path = movie_dir / link_name
            print(f"  Detected alternate cut ({duration_diff_min:.0f}m difference) → {suffix}")
            if link_path.exists():
                if link_path.stat().st_ino == main_feature.stat().st_ino:
                    print(f"  Already linked (same file): {link_path}")
                    return
                # Alternate cut path also taken — true conflict
                write_conflict(disc_dir, [{"library_path": str(link_path), "inode": link_path.stat().st_ino}])
                notify(
                    f"{title} ({suffix}): link conflict",
                    f"Library already has a different copy:\n{link_path}\n\nNew rip: {main_feature}\nResolve manually.",
                    error=True,
                )
                return
        else:
            print(f"  Conflict: {link_path} exists with different inode")
            write_conflict(disc_dir, [{"library_path": str(link_path), "inode": existing_inode}])
            notify(
                f"{title}: link conflict",
                f"Library already has a different copy:\n{link_path}\n\nNew rip: {main_feature}\nResolve manually.",
                error=True,
            )
            return

    action = "would link" if args.dry_run else "link"
    print(f"  {action}: {main_feature.name} → movies/{folder_name}/{link_name}")

    if not args.dry_run:
        movie_dir.mkdir(parents=True, exist_ok=True)
        os.link(main_feature, link_path)

    # Link extras too
    for i, extra in enumerate(extras):
        extra_size = extra.stat().st_size / (1024**3)
        extra_name = f"{title_safe} - Extra {i + 1}.mkv"
        extra_path = movie_dir / extra_name
        if not extra_path.exists():
            print(f"  {action}: {extra.name} → movies/{folder_name}/{extra_name} ({extra_size:.1f} GB)")
            if not args.dry_run:
                os.link(extra, extra_path)

    # Manifest
    if not args.dry_run:
        manifest = {
            "title": title,
            "year": year,
            "tmdb_id": movie.get("id"),
            "main_feature": main_feature.name,
            "extras": [e.name for e in extras],
            "timestamp": datetime.now().isoformat(),
        }
        manifest_path = disc_dir / ".movie-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  Manifest written: {manifest_path}")

    body = f"{title} ({year})\n{main_size_gb:.1f} GB main feature"
    if extras:
        body += f"\n{len(extras)} extra(s)"
    settle_identify(
        disc_dir,
        status="success",
        summary=f"{title} ({year}) linked",
        notify_title=f"{title}: linked to library",
        notify_body=body,
    )
    print(f"Done: {folder_name}")


if __name__ == "__main__":
    main()
