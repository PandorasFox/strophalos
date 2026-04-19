"""TMDb API client — movie/TV search, episode group fetching."""

from __future__ import annotations

import os
import re

from strophalos.backends.kagi import search_disc_title
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


def search_movie(label: str, duration_seconds: float = 0) -> dict | None:
    """Search TMDb for a movie. Returns best result dict or None.

    When *duration_seconds* is provided, fetches runtimes for the top results
    and prefers the candidate whose runtime is closest to the file duration.
    This prevents short OVAs from outranking feature films when the disc label
    is ambiguous (e.g. ``FINAL_FANTASY_VII``).
    """
    query = clean_movie_label(label)
    if not query:
        return None

    data = _tmdb_get("/search/movie", {"query": query})
    if not data:
        return None

    results = data.get("results", [])
    if not results:
        print(f"  TMDb: no movie results for '{query}'")
        # Fallback: web search for the mangled label, then re-query TMDb
        kagi_title = search_disc_title(label, media_type="movie")
        if kagi_title and kagi_title.lower() != query.lower():
            print(f"  TMDb: retrying with Kagi-resolved title '{kagi_title}'")
            data = _tmdb_get("/search/movie", {"query": kagi_title})
            if data:
                results = data.get("results", [])
        if not results:
            return None

    # Without a duration hint, just take the top result.
    if not duration_seconds or len(results) == 1:
        top = results[0]
        print(f"  TMDb: matched '{query}' → {top.get('title')} ({top.get('release_date', '?')[:4]})")
        return top

    # Fetch runtimes for the top candidates and pick the closest match.
    candidates = results[:5]
    best = candidates[0]
    best_diff = float("inf")

    for r in candidates:
        movie_id = r.get("id")
        if not movie_id:
            continue
        detail = _tmdb_get(f"/movie/{movie_id}")
        if not detail:
            continue
        runtime_min = detail.get("runtime") or 0
        runtime_sec = runtime_min * 60
        r["_runtime_sec"] = runtime_sec

        if runtime_sec <= 0:
            continue

        diff = abs(duration_seconds - runtime_sec)
        title_name = r.get("title", "?")
        print(f"  TMDb: candidate '{title_name}' runtime={runtime_min}m (diff={diff / 60:.0f}m)")

        if diff < best_diff:
            best_diff = diff
            best = r

    print(f"  TMDb: matched '{query}' → {best.get('title')} ({best.get('release_date', '?')[:4]})")
    return best


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
        kagi_title = search_disc_title(label, media_type="tv")
        if kagi_title and kagi_title.lower() != query.lower():
            print(f"  TMDb: retrying with Kagi-resolved title '{kagi_title}'")
            data = _tmdb_get("/search/tv", {"query": kagi_title})
            if data:
                results = data.get("results", [])
        if not results:
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


def fetch_all_episodes(label: str) -> tuple[list[Episode], list[Episode], str | None, str | None, int | None]:
    """Fetch all episodes for a series.

    Returns (regular_episodes, specials, group_name, series_name, series_id).
    Regular episodes sorted by (season, episode); specials sorted separately.
    """
    result = search_series(label)
    if not result:
        return [], [], None, None, None

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

    return regular, specials, group_name, series_name, series_id


# ---------------------------------------------------------------------------
# Disc classification helpers
# ---------------------------------------------------------------------------


def score_title_search(
    disc_label: str | None,
    durations: dict[int, int] | None = None,
) -> tuple[float, float]:
    """Query TMDb for the disc title. Returns (movie_boost, tv_boost).

    If durations are provided and TMDb returns a movie result with a runtime,
    checks if any title's duration matches the movie runtime (within 5%).
    A runtime match is a strong movie signal.

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

    movie_boost = 0.0
    tv_boost = 0.0

    if tv_count > movie_count:
        tv_boost = 0.3
    elif movie_count > tv_count:
        movie_boost = 0.3

    # Runtime matching: if top result is a movie, check if any title matches
    if durations and movie_count > 0:
        for r in top:
            if r.get("media_type") != "movie":
                continue
            # Fetch movie details for runtime
            movie_id = r.get("id")
            if not movie_id:
                continue
            detail = _tmdb_get(f"/movie/{movie_id}")
            if not detail:
                continue
            runtime_min = detail.get("runtime")
            if not runtime_min:
                continue
            runtime_sec = runtime_min * 60
            # Check if any title duration matches within 5%
            for tid, dur in durations.items():
                if runtime_sec > 0 and abs(dur - runtime_sec) / runtime_sec < 0.05:
                    title_name = r.get("title", "?")
                    print(f"  TMDb: runtime match — title {tid} ({dur}s) ≈ {title_name} ({runtime_sec}s)")
                    movie_boost = max(movie_boost, 0.6)
                    break
            if movie_boost >= 0.6:
                break

    return movie_boost, tv_boost
