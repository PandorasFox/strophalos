"""MusicBrainz API client — release search + duration matching."""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from strophalos.core.http import get_json

MB_USER_AGENT = "strophalos/1.0"


def _mb_base() -> str:
    """MusicBrainz API base URL. Set MB_SERVER to override (e.g. 'mb.example.com')."""
    server = os.environ.get("MB_SERVER", "")
    if not server:
        return "https://musicbrainz.org"
    if server.startswith("http://") or server.startswith("https://"):
        return server.rstrip("/")
    return f"https://{server}"


def _mb_get(path: str) -> dict[str, Any] | None:
    """GET a MusicBrainz API endpoint."""
    url = f"{_mb_base()}/ws/2{path}"
    return get_json(url, headers={"User-Agent": MB_USER_AGENT}, timeout=10)


def _search_releases(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search MB for releases by query string. Returns raw release list."""
    url = f"{_mb_base()}/ws/2/release/?query={urllib.parse.quote(query)}&fmt=json&limit={limit}"
    data = get_json(url, headers={"User-Agent": MB_USER_AGENT}, timeout=10)
    if not data:
        return []
    return data.get("releases", [])


def _fetch_release_detail(release_id: str) -> dict[str, Any] | None:
    """Fetch full release detail with recordings, media, and artist credits."""
    url = f"{_mb_base()}/ws/2/release/{release_id}?inc=recordings+media+artist-credits&fmt=json"
    return get_json(url, headers={"User-Agent": MB_USER_AGENT}, timeout=10)


def _get_artist(rel: dict[str, Any]) -> str:
    """Extract artist name from a release dict."""
    credits = rel.get("artist-credit", [])
    if credits:
        # Could be nested under "artist" key or directly on the credit
        credit = credits[0]
        return credit.get("name", "") or credit.get("artist", {}).get("name", "")
    return ""


def _filter_bluray_releases(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter releases to only those with Blu-ray media."""
    bd_releases = []
    for rel in releases:
        for medium in rel.get("media", []):
            fmt = (medium.get("format") or "").lower()
            if "blu-ray" in fmt:
                bd_releases.append(rel)
                break
    return bd_releases


def _pick_best_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """From a list of releases, prefer eng language, then first result."""
    if not releases:
        return None
    # Prefer English pseudo-release
    for rel in releases:
        lang = rel.get("text-representation", {}).get("language", "")
        if lang == "eng":
            return rel
    return releases[0]


def _get_release_tracks(rel: dict[str, Any], release_id: str) -> list[dict[str, Any]]:
    """Get track list with durations, fetching detail if needed."""
    tracks: list[dict[str, Any]] = []

    # Try from search result media first
    has_lengths = False
    for medium in rel.get("media", []):
        for track in medium.get("tracks", []):
            length_ms = track.get("length")
            if length_ms:
                has_lengths = True
            rec = track.get("recording", {})
            tracks.append({
                "position": track.get("position", track.get("number", "?")),
                "title": rec.get("title", track.get("title", "?")),
                "recording_id": rec.get("id", ""),
                "duration": int(length_ms) / 1000.0 if length_ms else 0.0,
            })

    if tracks and has_lengths:
        return tracks

    # Fetch full detail for recording lengths
    detail = _fetch_release_detail(release_id)
    if not detail:
        return tracks

    tracks = []
    for medium in detail.get("media", []):
        for track in medium.get("tracks", []):
            rec = track.get("recording", {})
            length_ms = rec.get("length") or track.get("length")
            tracks.append({
                "position": track.get("position", track.get("number", "?")),
                "title": rec.get("title", track.get("title", "?")),
                "recording_id": rec.get("id", ""),
                "duration": int(length_ms) / 1000.0 if length_ms else 0.0,
            })

    return tracks


# ---------------------------------------------------------------------------
# Release search (for identify-music: post-rip audio BD identification)
# ---------------------------------------------------------------------------


def search_release(label: str, durations: list[float], prefer_bluray: bool = False) -> dict[str, Any] | None:
    """Search MusicBrainz for a release matching the disc label + track durations.

    If prefer_bluray is True, filters results to Blu-ray releases and prefers
    eng language. Falls back to all releases if no Blu-ray results found.

    Returns {id, title, artist, tracks: [{position, title, recording_id, duration}]} or None.
    """
    query = label.replace("_", " ").strip()
    if not query:
        return None

    releases = _search_releases(f"release:{query}")
    if not releases:
        print(f"  MusicBrainz: no results for '{query}'")
        return None

    # Filter to Blu-ray if requested
    candidates = releases
    if prefer_bluray:
        bd_releases = _filter_bluray_releases(releases)
        if bd_releases:
            candidates = [_pick_best_release(bd_releases)]  # type: ignore[list-item]
            print(f"  MusicBrainz: filtered to {len(bd_releases)} Blu-ray release(s), using {candidates[0]['title']}")

    n_tracks = len(durations)
    sorted_durs = sorted(durations)

    for rel in candidates:
        if rel is None:
            continue
        rel_id = rel.get("id", "")
        rel_title = rel.get("title", "")
        artist = _get_artist(rel)

        tracks = _get_release_tracks(rel, rel_id)
        if not artist:
            detail = _fetch_release_detail(rel_id)
            if detail:
                artist = _get_artist(detail)

        if not tracks:
            continue

        # For exact count match, compare per-track durations
        if len(tracks) == n_tracks:
            track_durs = sorted(t["duration"] for t in tracks)
            total_diff = sum(abs(a - b) for a, b in zip(sorted_durs, track_durs))
            avg_diff = total_diff / n_tracks
            if avg_diff < 5.0:
                print(f"  MusicBrainz: {artist} - {rel_title} ({len(tracks)} tracks, avg diff {avg_diff:.1f}s)")
                return {
                    "id": rel_id,
                    "title": rel_title,
                    "artist": artist,
                    "tracks": tracks,
                }

        # For count mismatch (e.g. BD chapter splits), match by total duration
        mb_total = sum(t["duration"] for t in tracks)
        file_total = sum(sorted_durs)
        if mb_total > 0 and file_total > 0:
            diff_pct = abs(mb_total - file_total) / mb_total
            if diff_pct < 0.02:
                print(f"  MusicBrainz: {artist} - {rel_title} "
                      f"({len(tracks)} tracks, total duration match {diff_pct:.1%} off)")
                return {
                    "id": rel_id,
                    "title": rel_title,
                    "artist": artist,
                    "tracks": tracks,
                }

    print("  MusicBrainz: no duration match found")
    return None


# ---------------------------------------------------------------------------
# Disc classification helper (for rip-video: is this an audio BD?)
# ---------------------------------------------------------------------------


def score_musicbrainz(disc_label: str | None, durations: dict[int, int]) -> tuple[float, dict[str, Any] | None]:
    """Check MusicBrainz for a matching audio BD release. Returns (score, release_info).

    Searches by disc label, filters to Blu-ray releases, prefers English.
    Matches by total duration (since makemkv groups BD tracks into titles by
    playlist structure, so track counts won't match).
    """
    if not disc_label:
        return 0.0, None

    query = disc_label.replace("_", " ").strip()
    if not query:
        return 0.0, None

    releases = _search_releases(f'release:"{query}"')
    if not releases:
        return 0.0, None

    # Filter to Blu-ray releases only
    bd_releases = _filter_bluray_releases(releases)
    if not bd_releases:
        # Fall back to unfiltered if no Blu-ray results
        bd_releases = releases

    best = _pick_best_release(bd_releases)
    if best is None:
        return 0.0, None

    rel_id = best.get("id", "")
    rel_title = best.get("title", "")
    artist = _get_artist(best)

    tracks = _get_release_tracks(best, rel_id)
    if not artist:
        detail = _fetch_release_detail(rel_id)
        if detail:
            artist = _get_artist(detail)

    if not tracks:
        return 0.0, None

    # Total duration of all MB tracks
    mb_total = sum(t["duration"] for t in tracks)
    if mb_total == 0:
        return 0.0, None

    # For audio BDs, makemkv groups tracks into titles. The longest title
    # is usually the play-all (all tracks concatenated). Compare that against
    # the MB total, OR compare the sum of all titles (minus play-all duplicates).
    title_durs = sorted(durations.values(), reverse=True)

    # Strategy 1: longest title (play-all) vs MB total
    play_all_dur = title_durs[0]
    play_all_diff = abs(play_all_dur - mb_total) / mb_total if mb_total > 0 else 1.0

    # Strategy 2: sum of non-play-all titles vs MB total
    # (if play-all exists, the other titles are sections that should sum to ~play-all)
    non_play_all = title_durs[1:] if len(title_durs) > 1 else title_durs
    sections_total = sum(non_play_all)
    sections_diff = abs(sections_total - mb_total) / mb_total if mb_total > 0 else 1.0

    best_diff = min(play_all_diff, sections_diff)
    method = "play-all" if play_all_diff <= sections_diff else "sections"

    if best_diff < 0.02:  # within 2%
        score = max(0.0, 1.0 - best_diff * 50)  # 0% diff = 1.0, 2% diff = 0.0
        release_info = {
            "artist": artist,
            "title": rel_title,
            "id": rel_id,
            "track_count": len(tracks),
            "match_method": method,
            "duration_diff_pct": f"{best_diff:.1%}",
        }
        print(
            f"  MusicBrainz: matched '{query}' → {artist} - {rel_title}"
            f" ({len(tracks)} tracks, {method} match, {best_diff:.1%} off)"
        )
        return score, release_info

    if best_diff < 0.05:
        print(f"  MusicBrainz: weak match for '{query}' — {artist} - {rel_title} ({best_diff:.1%} off via {method})")

    return 0.0, None
