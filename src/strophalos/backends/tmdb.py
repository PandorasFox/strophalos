"""TMDb API client — movie/TV search, episode group fetching."""

from __future__ import annotations

import os
import re

from strophalos.core.http import build_url, get_json
from strophalos.types import Episode

API_BASE = "https://api.themoviedb.org/3"


def _tmdb_get(endpoint: str, params: dict[str, str] | None = None) -> dict | None:
    """Make a GET request to TMDb API v3. Returns parsed JSON or None on failure."""
    api_key = os.environ.get("TMDB_API_KEY", "")
    if not api_key:
        return None

    all_params = {"api_key": api_key}
    if params:
        all_params.update(params)

    return get_json(build_url(API_BASE, endpoint, all_params))


def clean_movie_label(label: str) -> str:
    """Clean a disc label for movie search (slightly less aggressive)."""
    name = label.replace("_", " ")
    name = re.sub(r"\s*(DISC\s*\d+|BDMV|BD|DVD|UHD)\s*$", "", name, flags=re.IGNORECASE)
    return name.strip()


# ---------------------------------------------------------------------------
# Movie search
# ---------------------------------------------------------------------------


def search_movie(label: str) -> dict | None:
    """Search TMDb for a movie. Returns top result dict or None."""
    query = clean_movie_label(label)
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


# ---------------------------------------------------------------------------
# TV series search + episode fetching
# ---------------------------------------------------------------------------


def search_series(label: str) -> tuple[int, str] | None:
    """Search TMDb for a TV series. Returns (series_id, name) or None."""
    query = label.replace("_", " ").strip()
    query = re.sub(r"\s*(S\d+|D\d+|DISC\s*\d+|BDMV|BD|DVD|UHD)\s*$", "", query, flags=re.IGNORECASE).strip()
    if not query:
        return None

    data = _tmdb_get("/search/tv", {"query": query})
    if not data:
        return None

    results = data.get("results", [])
    if not results:
        print(f"  TMDb: no TV results for '{query}'")
        return None

    top = results[0]
    series_id = top["id"]
    name = top.get("name", query)
    print(f"  TMDb: matched '{query}' → {name} (id={series_id})")
    return series_id, name


def fetch_episodes_from_group(series_id: int) -> list[Episode] | None:
    """Try to fetch episodes using DVD/BD episode group (type 3)."""
    data = _tmdb_get(f"/tv/{series_id}/episode_groups")
    if not data:
        return None

    groups = data.get("results", [])
    dvd_group = None
    for g in groups:
        if g.get("type") == 3:  # DVD order
            dvd_group = g
            break

    if not dvd_group:
        return None

    group_id = dvd_group["id"]
    print(f"  TMDb: using DVD episode group '{dvd_group.get('name', '')}' ({group_id})")

    group_data = _tmdb_get(f"/tv/episode_group/{group_id}")
    if not group_data:
        return None

    episodes: list[Episode] = []
    for season_group in group_data.get("groups", []):
        season_num = season_group.get("order", 1)
        for ep in season_group.get("episodes", []):
            runtime = (ep.get("runtime") or 0) * 60  # TMDb returns minutes
            episodes.append(
                Episode(
                    season=season_num,
                    episode=ep.get("order", ep.get("episode_number", 0)) + 1,
                    title=ep.get("name", ""),
                    runtime_seconds=runtime,
                )
            )

    return episodes if episodes else None


def fetch_episodes_standard(series_id: int) -> list[Episode]:
    """Fetch episodes using standard season ordering (fallback)."""
    series_data = _tmdb_get(f"/tv/{series_id}")
    if not series_data:
        return []

    n_seasons = series_data.get("number_of_seasons", 0)
    episodes: list[Episode] = []

    for s in range(1, n_seasons + 1):
        season_data = _tmdb_get(f"/tv/{series_id}/season/{s}")
        if not season_data:
            continue
        for ep in season_data.get("episodes", []):
            runtime = (ep.get("runtime") or 0) * 60
            episodes.append(
                Episode(
                    season=s,
                    episode=ep.get("episode_number", 0),
                    title=ep.get("name", ""),
                    runtime_seconds=runtime,
                )
            )

    return episodes


def fetch_all_episodes(label: str) -> tuple[list[Episode], list[Episode], str | None, str | None]:
    """Fetch all episodes for a series.

    Returns (regular_episodes, specials, group_name, series_name).
    Regular episodes sorted by (season, episode); specials sorted separately.
    """
    result = search_series(label)
    if not result:
        return [], [], None, None

    series_id, series_name = result

    # Prefer DVD/BD episode group
    eps = fetch_episodes_from_group(series_id)
    group_name: str | None = "DVD Order"
    if not eps:
        print("  TMDb: no DVD episode group, using standard ordering")
        eps = fetch_episodes_standard(series_id)
        group_name = None

    regular = sorted([ep for ep in eps if ep.season > 0], key=lambda e: (e.season, e.episode))
    specials = sorted([ep for ep in eps if ep.season == 0], key=lambda e: e.episode)

    return regular, specials, group_name, series_name


# ---------------------------------------------------------------------------
# Disc classification helpers
# ---------------------------------------------------------------------------


def score_title_search(disc_label: str | None) -> tuple[float, float]:
    """Query TMDb for the disc title. Returns (movie_boost, tv_boost).

    Requires TMDB_API_KEY env var; returns (0, 0) if unset or on failure.
    """

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key or not disc_label:
        return 0.0, 0.0

    query = disc_label.replace("_", " ").strip()
    if not query:
        return 0.0, 0.0

    url = build_url(API_BASE, "/search/multi", {"api_key": api_key, "query": query})
    data = get_json(url, timeout=5)
    if not data:
        return 0.0, 0.0

    results = data.get("results", [])
    if not results:
        print(f"  TMDb: no results for '{query}'")
        return 0.0, 0.0

    top = results[:3]
    types = [r.get("media_type", "") for r in top]
    names = [r.get("title") or r.get("name", "?") for r in top]
    print(f"  TMDb: {list(zip(names, types, strict=True))}")

    tv_count = types.count("tv")
    movie_count = types.count("movie")

    if tv_count > movie_count:
        return 0.0, 0.3
    elif movie_count > tv_count:
        return 0.3, 0.0

    return 0.0, 0.0
