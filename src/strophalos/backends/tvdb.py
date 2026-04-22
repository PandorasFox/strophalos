"""TheTVDB API v4 client — episode fetching as supplement to TMDb."""

from __future__ import annotations

import os

from strophalos.core.http import build_url, get_json, post_json
from strophalos.types import Episode

API_BASE = "https://api4.thetvdb.com/v4"

_token: str | None = None


def _authenticate() -> str | None:
    """Obtain a bearer token (cached for the process lifetime)."""
    global _token
    if _token:
        return _token

    api_key = os.environ.get("TVDB_API_KEY", "")
    if not api_key:
        return None

    resp = post_json(f"{API_BASE}/login", {"apikey": api_key})
    if not resp or resp.get("status") != "success":
        print("  TVDB: authentication failed")
        return None

    _token = resp["data"]["token"]
    return _token


def _tvdb_get(endpoint: str, params: dict[str, str] | None = None) -> dict | None:
    """Make an authenticated GET request to TheTVDB API v4."""
    token = _authenticate()
    if not token:
        return None

    url = build_url(API_BASE, endpoint, params)
    return get_json(url, headers={"Authorization": f"Bearer {token}"})


def search_series(name: str) -> int | None:
    """Search TheTVDB for a TV series by name. Returns TVDB series ID or None."""
    data = _tvdb_get("/search", {"query": name, "type": "series"})
    if not data or data.get("status") != "success":
        return None

    results = data.get("data", [])
    if not results:
        return None

    # Take the first result
    tvdb_id = results[0].get("tvdb_id") or results[0].get("id")
    if tvdb_id:
        return int(tvdb_id)
    return None


def fetch_season_episodes(series_id: int, season: int) -> list[Episode]:
    """Fetch episodes for a specific season from TheTVDB.

    Uses the 'default' season type (aired order).
    """
    episodes: list[Episode] = []
    page = 0

    while True:
        data = _tvdb_get(
            f"/series/{series_id}/episodes/default",
            {"page": str(page), "season": str(season)},
        )
        if not data or data.get("status") != "success":
            break

        page_eps = data.get("data", {}).get("episodes", [])
        if not page_eps:
            break

        for ep in page_eps:
            runtime_min = ep.get("runtime") or 0
            episodes.append(
                Episode(
                    season=ep.get("seasonNumber", season),
                    episode=ep.get("number", 0),
                    title=ep.get("name", ""),
                    runtime_seconds=runtime_min * 60,
                )
            )

        page += 1

    return sorted(episodes, key=lambda e: (e.season, e.episode))


def supplement_episodes(
    series_name: str,
    season: int,
    tmdb_episodes: list[Episode],
) -> list[Episode] | None:
    """Check TheTVDB for a better episode list when TMDb may be wrong.

    Returns TVDB episodes if they provide more episodes for the season,
    or None if TMDb's list is fine (or TVDB is unavailable).
    """
    if not os.environ.get("TVDB_API_KEY"):
        return None

    tvdb_id = search_series(series_name)
    if not tvdb_id:
        print(f"  TVDB: no match for '{series_name}'")
        return None

    tvdb_eps = fetch_season_episodes(tvdb_id, season)
    tmdb_season_count = len([ep for ep in tmdb_episodes if ep.season == season])
    tvdb_season_count = len(tvdb_eps)

    if tvdb_season_count <= tmdb_season_count:
        return None

    print(
        f"  TVDB: {tvdb_season_count} episodes for season {season} "
        f"(vs TMDb {tmdb_season_count}) — using TVDB episode list"
    )
    return tvdb_eps
