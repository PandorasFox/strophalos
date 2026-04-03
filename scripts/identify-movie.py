#!/usr/bin/env python3
"""Post-rip movie identification — hard-links the main feature to the library.

Searches TMDb by disc label, gets canonical title + year, and creates a
hard link in the Jellyfin/Plex-friendly structure:
  /media/library/movies/{Title} ({year})/{Title}.mkv

Usage: identify-movie.py --dir /media/archive/movies/rips/bd/LABEL/disc1 --label LABEL
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
API_BASE = "https://api.themoviedb.org/3"


def _notify(title: str, body: str, error: bool = False) -> None:
    try:
        cmd = [NOTIFY_SCRIPT]
        if error:
            cmd.append("--error")
        cmd.extend([title, body])
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass


def _tmdb_get(endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
    import urllib.parse
    import urllib.request

    api_key = os.environ.get("TMDB_API_KEY", "")
    if not api_key:
        return None

    all_params = {"api_key": api_key}
    if params:
        all_params.update(params)

    url = f"{API_BASE}{endpoint}?{urllib.parse.urlencode(all_params)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  TMDb request failed: {e}")
        return None


def _clean_label(label: str) -> str:
    name = label.replace("_", " ")
    name = re.sub(r"\s*(DISC\s*\d+|BDMV|BD|DVD|UHD)\s*$", "", name, flags=re.IGNORECASE)
    return name.strip()


def sanitize_filename(name: str) -> str:
    name = name.replace(":", " -")
    name = re.sub(r'[?*<>|"\\]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def search_movie(label: str) -> dict[str, Any] | None:
    """Search TMDb for a movie. Returns top result dict or None."""
    query = _clean_label(label)
    if not query:
        return None

    data = _tmdb_get("/search/movie", {"query": query})
    if not data:
        return None

    results = data.get("results", [])
    if not results:
        print(f"  TMDb: no movie results for '{query}'")
        return None

    top = results[0]
    print(f"  TMDb: matched '{query}' → {top.get('title')} ({top.get('release_date', '?')[:4]})")
    return top


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

    # Search TMDb
    movie = search_movie(args.label)
    if not movie:
        clean = _clean_label(args.label)
        msg = f"No TMDb match for '{clean}'. Add the movie at https://www.themoviedb.org and re-run."
        print(f"  {msg}")
        _notify(f"{clean}: identification failed", msg, error=True)
        return

    title = movie.get("title", _clean_label(args.label))
    year = movie.get("release_date", "")[:4]
    title_safe = sanitize_filename(title)
    folder_name = f"{title_safe} ({year})" if year else title_safe

    # Library path
    movie_dir = library / "movies" / folder_name
    link_name = f"{title_safe}.mkv"
    link_path = movie_dir / link_name

    if link_path.exists():
        print(f"  Already linked: {link_path}")
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
    _notify(f"{title}: linked to library", body)
    print(f"Done: {folder_name}")


if __name__ == "__main__":
    main()
