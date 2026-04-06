"""OpenSubtitles v2 API — hash-based episode identification."""

from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path
from typing import Any

from strophalos.core.http import get_json

OPENSUBTITLES_CONFIG = Path("/config/opensubtitles.json")
OPENSUBTITLES_API_BASE = "https://api.opensubtitles.com/api/v1"


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
    """
    from strophalos.core.notify import notify

    config = _load_config()
    if config is None:
        return None

    if not _token_valid(config):
        print("  OpenSubtitles: token invalid. Re-run setup-opensubtitles to re-authenticate.")
        notify("OpenSubtitles token invalid", "Run: docker exec -it strophalos setup-opensubtitles", error=True)
        return None

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
