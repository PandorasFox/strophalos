"""MusicBrainz API client — release search + duration matching."""

from __future__ import annotations

import os
from typing import Any

from strophalos.core.http import get_json

MB_USER_AGENT = "strophalos/1.0"


def _mb_server() -> str:
    return os.environ.get("MB_SERVER", "mb-web:5000")


def _mb_get(path: str) -> dict[str, Any] | None:
    """GET a MusicBrainz API endpoint."""
    url = f"http://{_mb_server()}/ws/2{path}"
    return get_json(url, headers={"User-Agent": MB_USER_AGENT}, timeout=5)


# ---------------------------------------------------------------------------
# Release search (for identify-music: post-rip audio BD identification)
# ---------------------------------------------------------------------------


def search_release(label: str, durations: list[float]) -> dict[str, Any] | None:
    """Search MusicBrainz for a release matching the disc label + track durations.

    Returns {id, title, artist, tracks: [{position, title, recording_id, duration}]} or None.
    """
    import urllib.parse

    query = label.replace("_", " ").strip()
    if not query:
        return None

    url = f"http://{_mb_server()}/ws/2/release/?query=release:{urllib.parse.quote(query)}&fmt=json&limit=10"
    data = get_json(url, headers={"User-Agent": MB_USER_AGENT}, timeout=5)
    if not data:
        return None

    releases = data.get("releases", [])
    if not releases:
        print(f"  MusicBrainz: no results for '{query}'")
        return None

    n_tracks = len(durations)
    sorted_durs = sorted(durations)

    for rel in releases:
        rel_id = rel.get("id", "")
        rel_title = rel.get("title", "")
        artist = ""
        if rel.get("artist-credit"):
            artist = rel["artist-credit"][0].get("name", "")

        # Fetch full release with recordings
        detail_url = f"http://{_mb_server()}/ws/2/release/{rel_id}?inc=recordings+media+artist-credits&fmt=json"
        detail = get_json(detail_url, headers={"User-Agent": MB_USER_AGENT}, timeout=5)
        if not detail:
            continue

        # Extract artist from detail if not in search result
        if not artist and detail.get("artist-credit"):
            artist = detail["artist-credit"][0].get("artist", {}).get("name", "")

        # Build track list with durations
        tracks: list[dict[str, Any]] = []
        for medium in detail.get("media", []):
            for track in medium.get("tracks", []):
                rec = track.get("recording", {})
                length_ms = rec.get("length") or track.get("length")
                tracks.append(
                    {
                        "position": track.get("position", track.get("number", "?")),
                        "title": rec.get("title", track.get("title", "?")),
                        "recording_id": rec.get("id", ""),
                        "duration": int(length_ms) / 1000.0 if length_ms else 0.0,
                    }
                )

        if len(tracks) != n_tracks:
            continue

        # Check duration alignment
        track_durs = sorted(t["duration"] for t in tracks)
        total_diff = sum(abs(a - b) for a, b in zip(sorted_durs, track_durs, strict=True))
        avg_diff = total_diff / n_tracks

        if avg_diff < 5.0:
            print(f"  MusicBrainz: {artist} - {rel_title} ({len(tracks)} tracks, avg diff {avg_diff:.1f}s)")
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
    """Check MusicBrainz for a matching release. Returns (score, release_info).

    Searches by disc label against the MusicBrainz replica. If a release matches
    and track durations align with title durations, returns a high score.
    """
    import urllib.parse

    if not disc_label:
        return 0.0, None

    query = disc_label.replace("_", " ").strip()
    if not query:
        return 0.0, None

    url = f"http://{_mb_server()}/ws/2/release/?query=release:{urllib.parse.quote(query)}&fmt=json&limit=5"
    data = get_json(url, headers={"User-Agent": MB_USER_AGENT}, timeout=5)
    if not data:
        return 0.0, None

    releases = data.get("releases", [])
    if not releases:
        return 0.0, None

    title_durs = sorted(durations.values())
    n_titles = len(title_durs)

    best_score = 0.0
    best_release: dict[str, Any] | None = None

    for rel in releases:
        rel_id = rel.get("id", "")
        rel_title = rel.get("title", "")
        artist = ""
        if rel.get("artist-credit"):
            artist = rel["artist-credit"][0].get("name", "")

        # Get track durations from the release's media
        track_durs: list[float] = []
        for medium in rel.get("media", []):
            for track in medium.get("tracks", []):
                length_ms = track.get("length")
                if length_ms:
                    track_durs.append(length_ms / 1000.0)

        if not track_durs:
            # Need to fetch full release for track info
            detail_url = f"http://{_mb_server()}/ws/2/release/{rel_id}?inc=recordings+media&fmt=json"
            detail = get_json(detail_url, headers={"User-Agent": MB_USER_AGENT}, timeout=5)
            if not detail:
                continue
            for medium in detail.get("media", []):
                for track in medium.get("tracks", []):
                    rec = track.get("recording", {})
                    length_ms = rec.get("length") or track.get("length")
                    if length_ms:
                        track_durs.append(int(length_ms) / 1000.0)

        if not track_durs or len(track_durs) != n_titles:
            continue

        sorted_tracks = sorted(track_durs)
        total_diff = sum(abs(td - rd) for td, rd in zip(title_durs, sorted_tracks, strict=True))
        avg_diff = total_diff / n_titles

        if avg_diff < 5.0:
            score = max(0, 1.0 - avg_diff / 5.0)
            if score > best_score:
                best_score = score
                best_release = {
                    "artist": artist,
                    "title": rel_title,
                    "id": rel_id,
                    "track_count": len(track_durs),
                    "avg_diff": avg_diff,
                }

    if best_release:
        print(
            f"  MusicBrainz: matched '{query}' → {best_release['artist']} - {best_release['title']}"
            f" ({best_release['track_count']} tracks, avg diff {best_release['avg_diff']:.1f}s)"
        )

    return best_score, best_release
