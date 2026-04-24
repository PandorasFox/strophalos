"""OpenSubtitles v2 API — hash-based identification and reference subtitle fetching."""

from __future__ import annotations

import json
import os
import struct
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strophalos.core.http import get_json, post_json

OPENSUBTITLES_CONFIG = Path("/config/opensubtitles.json")
OPENSUBTITLES_API_BASE = "https://api.opensubtitles.com/api/v1"


class AuthError(Exception):
    """OpenSubtitles token is expired or invalid — re-run setup-opensubtitles."""


def compute_hash(path: Path) -> str | None:
    """Compute the OpenSubtitles hash for a file.

    Algorithm:
    - Start with file size as a 64-bit value
    - Add the first 65536 bytes interpreted as 64-bit little-endian integers
    - Add the last 65536 bytes interpreted as 64-bit little-endian integers
    - Mask to 64 bits and return as 16-char lowercase hex
    """
    BLOCK_SIZE = 65536

    try:
        file_size = path.stat().st_size
        if file_size < BLOCK_SIZE * 2:
            return None

        hash_val = file_size
        fmt = "<" + str(BLOCK_SIZE // 8) + "Q"

        with open(path, "rb") as f:
            buf = f.read(BLOCK_SIZE)
            hash_val += sum(struct.unpack(fmt, buf))

            f.seek(max(0, file_size - BLOCK_SIZE))
            buf = f.read(BLOCK_SIZE)
            hash_val += sum(struct.unpack(fmt, buf))

        hash_val &= 0xFFFFFFFFFFFFFFFF
        return f"{hash_val:016x}"
    except Exception as e:
        print(f"  OpenSubtitles: hash computation failed for {path.name}: {e}")
        return None


def _load_config() -> dict[str, Any] | None:
    """Load OpenSubtitles config (JWT token + API key). Returns None if unavailable."""
    api_key = os.environ.get("OPENSUBTITLES_API_KEY", "")
    if not api_key:
        return None

    if not OPENSUBTITLES_CONFIG.exists():
        return None

    try:
        config = json.loads(OPENSUBTITLES_CONFIG.read_text())
    except Exception:
        return None

    token = config.get("token", "")
    if not token:
        return None

    config["api_key"] = api_key
    return config


def _token_valid(config: dict[str, Any]) -> bool:
    """Check whether the stored JWT token is valid by hitting the API."""
    import urllib.request

    api_key = config.get("api_key", "")
    token = config.get("token", "")
    if not api_key or not token:
        return False

    try:
        req = urllib.request.Request(
            f"{OPENSUBTITLES_API_BASE}/infos/user",
            headers={
                "Api-Key": api_key,
                "Authorization": f"Bearer {token}",
                "User-Agent": "strophalos v1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _api_get(
    endpoint: str,
    params: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Make a GET request to the OpenSubtitles v2 API."""
    import urllib.parse

    api_key = config["api_key"]
    token = config["token"]

    qs = urllib.parse.urlencode(params)
    url = f"{OPENSUBTITLES_API_BASE}{endpoint}?{qs}"

    return get_json(
        url,
        headers={
            "Api-Key": api_key,
            "Authorization": f"Bearer {token}",
            "User-Agent": "strophalos v1.0",
        },
        timeout=15,
    )


def identify(mkv_files: list[Path]) -> dict[Path, tuple[int, int, str]] | None:
    """Identify episodes via OpenSubtitles hash lookup.

    Returns a mapping from file path to (season, episode, title) for every
    file that was successfully identified. Returns None if the OpenSubtitles
    integration is not configured or entirely unavailable.

    Raises AuthError if configured but the token is expired.
    """
    config = _load_config()
    if config is None:
        return None

    if not _token_valid(config):
        raise AuthError("OpenSubtitles token invalid. Re-run setup-opensubtitles to re-authenticate.")

    print("  OpenSubtitles: attempting hash-based identification...")
    results: dict[Path, tuple[int, int, str]] = {}

    for i, mkv in enumerate(mkv_files):
        # Rate limit: max 1 request per second
        if i > 0:
            time.sleep(1.0)

        file_hash = compute_hash(mkv)
        if not file_hash:
            continue

        data = _api_get("/subtitles", {"moviehash": file_hash}, config)
        if not data:
            continue

        entries = data.get("data", [])
        if not entries:
            print(f"    {mkv.name}: no hash match")
            continue

        # Find the best match — prefer entries with episode info
        best: dict[str, Any] | None = None
        for entry in entries:
            attrs = entry.get("attributes", {})
            feat = attrs.get("feature_details", {})
            if feat.get("season_number") is not None and feat.get("episode_number") is not None:
                best = entry
                break

        if best is None:
            best = entries[0]

        attrs = best.get("attributes", {})
        feat = attrs.get("feature_details", {})

        season = feat.get("season_number")
        episode = feat.get("episode_number")
        title = feat.get("title") or feat.get("parent_title") or ""

        if season is not None and episode is not None:
            results[mkv] = (int(season), int(episode), str(title))
            print(f"    {mkv.name}: S{int(season):02d}E{int(episode):02d} — {title}")
        else:
            print(f"    {mkv.name}: hash matched but no episode info (movie?)")

    if results:
        print(f"  OpenSubtitles: identified {len(results)}/{len(mkv_files)} file(s)")
    else:
        print("  OpenSubtitles: no files identified via hash lookup")

    return results if results else None


# ---------------------------------------------------------------------------
# Reference subtitle fetching — search, download, and cache
# ---------------------------------------------------------------------------

SUBTITLE_CACHE_DIR = Path("/config/subtitle-cache")
SUBTITLE_CACHE_INDEX = SUBTITLE_CACHE_DIR / "index.json"
NEGATIVE_CACHE_TTL_DAYS = 30


def _api_post(
    endpoint: str,
    data: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Make a POST request to the OpenSubtitles v2 API."""
    api_key = config["api_key"]
    token = config["token"]

    url = f"{OPENSUBTITLES_API_BASE}{endpoint}"
    return post_json(
        url,
        data,
        headers={
            "Api-Key": api_key,
            "Authorization": f"Bearer {token}",
            "User-Agent": "strophalos v1.0",
        },
        timeout=15,
    )


def _load_cache_index() -> dict[str, Any]:
    """Load the subtitle cache index. Returns empty dict if missing."""
    if not SUBTITLE_CACHE_INDEX.exists():
        return {}
    try:
        return json.loads(SUBTITLE_CACHE_INDEX.read_text())
    except Exception:
        return {}


def _save_cache_index(index: dict[str, Any]) -> None:
    """Write the subtitle cache index to disk."""
    SUBTITLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SUBTITLE_CACHE_INDEX.write_text(json.dumps(index, indent=2))


def _cache_key(series_id: int, season: int, episode: int) -> str:
    return f"tmdb_{series_id}_s{season:02d}e{episode:02d}"


def _is_negative_expired(entry: dict[str, Any]) -> bool:
    """Check if a negative cache entry has expired (>30 days old)."""
    fetched = entry.get("fetched_at", "")
    if not fetched:
        return True
    try:
        fetched_dt = datetime.fromisoformat(fetched)
        age = datetime.now(UTC) - fetched_dt
        return age.days > NEGATIVE_CACHE_TTL_DAYS
    except Exception:
        return True


def _search_episode_subs(
    tmdb_id: int,
    season: int,
    episode: int,
    config: dict[str, Any],
) -> int | None:
    """Search for the best subtitle file for an episode. Returns file_id or None."""
    data = _api_get(
        "/subtitles",
        {
            "tmdb_id": str(tmdb_id),
            "season_number": str(season),
            "episode_number": str(episode),
            "languages": "en",
        },
        config,
    )
    if not data:
        return None

    entries = data.get("data", [])
    if not entries:
        return None

    # Pick the file with the highest download count, preferring .srt
    best_file_id: int | None = None
    best_downloads = -1
    for entry in entries:
        attrs = entry.get("attributes", {})
        for f in attrs.get("files", []):
            file_id = f.get("file_id")
            # Use the entry-level download_count as proxy
            dl_count = attrs.get("download_count", 0)
            if file_id and dl_count > best_downloads:
                best_downloads = dl_count
                best_file_id = file_id

    return best_file_id


def _download_subtitle(file_id: int, config: dict[str, Any]) -> str | None:
    """Download a subtitle file. Returns .srt content as string, or None."""
    resp = _api_post("/download", {"file_id": file_id}, config)
    if not resp:
        return None

    link = resp.get("link")
    if not link:
        print(f"  OpenSubtitles: download response missing link for file_id={file_id}")
        return None

    try:
        req = urllib.request.Request(link, headers={"User-Agent": "strophalos v1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  OpenSubtitles: failed to download subtitle file: {e}")
        return None


def fetch_reference_subs(
    episodes: list[Any],
    series_id: int,
) -> dict[tuple[int, int], list[tuple[float, str]]]:
    """Fetch reference subtitles for candidate episodes.

    Returns {(season, episode): [(timestamp_seconds, text), ...]} for episodes
    where subtitles were found. Uses a local cache to avoid redundant downloads.

    Raises AuthError if configured but the token is expired.
    """
    from strophalos.identify.subtitles import parse_srt

    config = _load_config()
    if config is None:
        return {}

    if not _token_valid(config):
        raise AuthError("OpenSubtitles token invalid. Re-run setup-opensubtitles to re-authenticate.")

    cache_index = _load_cache_index()
    results: dict[tuple[int, int], list[tuple[float, str]]] = {}
    api_calls_made = 0

    print(f"  OpenSubtitles: fetching reference subs for {len(episodes)} episode(s)...")

    for ep in episodes:
        key = _cache_key(series_id, ep.season, ep.episode)
        srt_path = SUBTITLE_CACHE_DIR / f"{key}.srt"

        # Check cache
        if key in cache_index:
            entry = cache_index[key]
            if entry.get("not_found"):
                if not _is_negative_expired(entry):
                    continue
                # Expired negative — re-query
            else:
                # Positive cache hit — read from disk
                if srt_path.exists():
                    cues = parse_srt(srt_path.read_text(errors="replace"))
                    if cues:
                        results[(ep.season, ep.episode)] = cues
                    continue

        # Rate limit
        if api_calls_made > 0:
            time.sleep(1.0)

        # Search
        file_id = _search_episode_subs(series_id, ep.season, ep.episode, config)
        api_calls_made += 1

        if file_id is None:
            cache_index[key] = {
                "not_found": True,
                "fetched_at": datetime.now(UTC).isoformat(),
            }
            print(f"    {ep.code}: no subs available")
            continue

        # Download
        time.sleep(1.0)
        srt_content = _download_subtitle(file_id, config)
        api_calls_made += 1

        if srt_content is None:
            continue

        # Cache to disk
        SUBTITLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        srt_path.write_text(srt_content)
        cache_index[key] = {
            "file_id": file_id,
            "language": "en",
            "fetched_at": datetime.now(UTC).isoformat(),
            "not_found": False,
        }

        cues = parse_srt(srt_content)
        if cues:
            results[(ep.season, ep.episode)] = cues
            print(f"    {ep.code}: {len(cues)} cue(s) cached")
        else:
            print(f"    {ep.code}: downloaded but no parseable cues")

    _save_cache_index(cache_index)
    print(f"  OpenSubtitles: {len(results)}/{len(episodes)} episode(s) with reference subs")
    return results
