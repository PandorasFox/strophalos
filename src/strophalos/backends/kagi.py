"""Kagi Search API — fallback title resolution for mangled disc labels."""

from __future__ import annotations

import os
import re

from strophalos.core.cache import _MISS, DiskCache
from strophalos.core.fs import strip_pressing_code
from strophalos.core.http import build_url, get_json

API_BASE = "https://kagi.com/api/v0"
_cache = DiskCache("kagi")


def search_disc_title(label: str, media_type: str = "movie") -> str | None:
    """Search Kagi for a disc label and try to extract the real title.

    Returns a cleaned title string suitable for re-querying TMDb, or None.
    *media_type* should be "movie" or "tv" to guide the search terms.
    """
    api_key = os.environ.get("KAGI_API_KEY")
    if not api_key:
        return None

    query = (strip_pressing_code(label) or label).replace("_", " ").strip()
    if not query:
        return None

    media_hint = "blu-ray" if media_type == "movie" else "tv series blu-ray"
    search_query = f"{query} {media_hint}"

    cached = _cache.get(search_query)
    if cached is not _MISS:
        print(f"  Kagi: cache hit for '{search_query}'")
        results = cached or []
    else:
        url = build_url(API_BASE, "/search", {"q": search_query, "limit": "5"})
        data = get_json(url, headers={"Authorization": f"Bot {api_key}"})
        if not data:
            return None
        results = data.get("data", [])
        _cache.put(search_query, results)

    # Only look at actual search results (t=0), skip related searches (t=1)
    search_results = [r for r in results if r.get("t") == 0]
    if not search_results:
        print(f"  Kagi: no results for '{search_query}'")
        return None

    # Extract title from the top result — strip common suffixes like
    # "4K Blu-ray", "| Blu-ray.com", "- Wikipedia", etc.
    raw_title = search_results[0].get("title", "")
    print(f"  Kagi: top result — '{raw_title}'")

    title = _extract_title(raw_title)
    if title:
        print(f"  Kagi: extracted title — '{title}'")
    return title


def _extract_title(raw: str) -> str | None:
    """Best-effort extraction of a movie/show title from a search result title."""
    # Strip site names after pipes, dashes-with-spaces, or colons at the end
    # e.g. "How to Train Your Dragon 4K Blu-ray | Blu-ray.com"
    # e.g. "Movie Title - Wikipedia"
    title = re.split(r"\s*[|]\s*", raw, maxsplit=1)[0]
    title = re.sub(r"\s+-\s+(?:Wikipedia|IMDb|Rotten Tomatoes|Metacritic|Amazon\.com).*$", "", title)

    # Strip format/edition suffixes
    title = re.sub(
        r"\s*\(?\b("
        r"4K|UHD|Blu-ray|DVD|BD|Ultra HD|Digital|Steelbook|"
        r"Collector.s Edition|Special Edition|Limited Edition|"
        r"Complete Series|Box Set"
        r")\b\)?.*$",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = title.strip(" -–—:")
    return title if title else None
