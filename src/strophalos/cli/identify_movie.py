"""CLI entry point for identify-movie — TMDb lookup + hard-link to library."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from strophalos.backends.tmdb import clean_movie_label, search_movie
from strophalos.core.fs import sanitize_filename
from strophalos.core.mkv import get_mkv_duration
from strophalos.core.notify import notify
from strophalos.daemon.manifest import write_conflict


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

    # Find the main feature (largest file)
    main_feature = max(mkv_files, key=lambda p: p.stat().st_size)
    main_size_gb = main_feature.stat().st_size / (1024**3)
    extras = [f for f in mkv_files if f != main_feature]

    print(f"  Main feature: {main_feature.name} ({main_size_gb:.1f} GB)")
    if extras:
        print(f"  Extras: {len(extras)} file(s)")

    # Search TMDb — pass main feature duration so ambiguous labels can be
    # disambiguated by runtime (e.g. 25-min OVA vs 101-min film).
    duration = get_mkv_duration(main_feature)
    if duration:
        print(f"  Duration: {duration / 60:.0f}m")
    movie = search_movie(args.label, duration_seconds=duration, mkv_title=main_feature.stem)
    if not movie:
        clean = clean_movie_label(args.label)
        msg = f"No TMDb match for '{clean}'. Add the movie at https://www.themoviedb.org and re-run."
        print(f"  {msg}")
        notify(f"{clean}: identification failed", msg, error=True)
        return

    title = movie.get("title", clean_movie_label(args.label))
    year = movie.get("release_date", "")[:4]
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
    notify(f"{title}: linked to library", body)
    print(f"Done: {folder_name}")


if __name__ == "__main__":
    main()
