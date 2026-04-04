#!/usr/bin/env python3
"""Post-rip episode identification for TV disc rips.

Matches ripped MKV files to TMDb episodes using multiple identification layers:
- OpenSubtitles hash lookup (file hash → episode metadata)
- AniDB ed2k hash lookup (anime identification via UDP API)
- SubDB hash lookup (subtitle download for text-matching fallback)
- Duration filtering and subtitle text scoring

Usage: identify-episodes.py --dir /output/bd/Show --label DISC_LABEL [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import struct
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Episode:
    season: int
    episode: int
    title: str
    runtime_seconds: float

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass
class RippedFile:
    path: Path
    duration_seconds: float
    subtitle_texts: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class MatchResult:
    """Result of identifying a single file."""
    file: RippedFile
    episode: Episode
    method: str  # "opensubtitles hash", "anidb hash", "subtitle match", "duration", "forward order"
    score: float = 0.0


# ---------------------------------------------------------------------------
# OpenSubtitles v2 API — hash-based episode identification
# ---------------------------------------------------------------------------

NOTIFY_SCRIPT = shutil.which("notify.sh") or "/usr/local/bin/notify.sh"
OPENSUBTITLES_CONFIG = Path("/config/opensubtitles.json")
OPENSUBTITLES_API_BASE = "https://api.opensubtitles.com/api/v1"


def _opensubtitles_hash(path: Path) -> str | None:
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
        fmt = "<" + str(BLOCK_SIZE // 8) + "Q"  # 8192 unsigned 64-bit LE ints

        with open(path, "rb") as f:
            # First 64KB
            buf = f.read(BLOCK_SIZE)
            hash_val += sum(struct.unpack(fmt, buf))

            # Last 64KB
            f.seek(max(0, file_size - BLOCK_SIZE))
            buf = f.read(BLOCK_SIZE)
            hash_val += sum(struct.unpack(fmt, buf))

        hash_val &= 0xFFFFFFFFFFFFFFFF
        return f"{hash_val:016x}"
    except Exception as e:
        print(f"  OpenSubtitles: hash computation failed for {path.name}: {e}")
        return None


def _load_opensubtitles_config() -> dict[str, Any] | None:
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


def _opensubtitles_token_valid(config: dict[str, Any]) -> bool:
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


def _opensubtitles_get(
    endpoint: str,
    params: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Make a GET request to the OpenSubtitles v2 API."""
    import urllib.parse
    import urllib.request

    api_key = config["api_key"]
    token = config["token"]

    qs = urllib.parse.urlencode(params)
    url = f"{OPENSUBTITLES_API_BASE}{endpoint}?{qs}"

    req = urllib.request.Request(url, headers={
        "Api-Key": api_key,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "strophalos v1.0",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  OpenSubtitles: API request failed: {e}")
        return None


def opensubtitles_identify(
    mkv_files: list[Path],
) -> dict[Path, tuple[int, int, str]] | None:
    """Identify episodes via OpenSubtitles hash lookup.

    Returns a mapping from file path to (season, episode, title) for every
    file that was successfully identified. Returns None if the OpenSubtitles
    integration is not configured or entirely unavailable.
    """
    config = _load_opensubtitles_config()
    if config is None:
        return None

    if not _opensubtitles_token_valid(config):
        print("  OpenSubtitles: token invalid. Re-run setup-opensubtitles.sh to re-authenticate.")
        _notify("OpenSubtitles token invalid", "Run: docker exec -it strophalos setup-opensubtitles.sh", error=True)
        return None

    print("  OpenSubtitles: attempting hash-based identification...")
    results: dict[Path, tuple[int, int, str]] = {}

    for i, mkv in enumerate(mkv_files):
        # Rate limit: max 1 request per second
        if i > 0:
            time.sleep(1.0)

        file_hash = _opensubtitles_hash(mkv)
        if not file_hash:
            continue

        data = _opensubtitles_get("/subtitles", {"moviehash": file_hash}, config)
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
            # Try the first entry even without episode details
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
# AniDB UDP API — ed2k hash-based anime identification
# ---------------------------------------------------------------------------

ANIDB_CONFIG = Path("/config/anidb.json")
ANIDB_CACHE = Path("/config/anidb-cache.json")
ANIDB_HOST = "api.anidb.net"
ANIDB_PORT = 9000
ANIDB_PROTO_VER = 3

# Check if MD4 is available (needed for ed2k hash)
_MD4_AVAILABLE = True
try:
    hashlib.new("md4", b"", usedforsecurity=False)
except ValueError:
    _MD4_AVAILABLE = False


def _ed2k_hash(path: Path) -> str | None:
    """Compute the ed2k hash (MD4-based) for AniDB file lookup.

    Split file into 9500 KiB chunks, MD4 each chunk.
    Single chunk → that MD4 is the hash. Multiple → MD4 of concatenated chunk hashes.
    """
    if not _MD4_AVAILABLE:
        return None

    CHUNK_SIZE = 9728000  # 9500 KiB

    try:
        file_size = path.stat().st_size
        if file_size == 0:
            return None

        chunk_hashes: list[bytes] = []
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h = hashlib.new("md4", chunk, usedforsecurity=False)
                chunk_hashes.append(h.digest())

        if len(chunk_hashes) == 1:
            return chunk_hashes[0].hex()

        h = hashlib.new("md4", b"".join(chunk_hashes), usedforsecurity=False)
        return h.digest().hex()
    except Exception as e:
        print(f"  AniDB: ed2k hash failed for {path.name}: {e}")
        return None


def _load_anidb_config() -> dict[str, Any] | None:
    """Load AniDB config from /config/anidb.json. Returns None if not configured.

    Expected format: {"username": "...", "api_password": "...",
                      "client": "strophalos", "clientver": 1}
    """
    if not ANIDB_CONFIG.exists():
        return None
    try:
        config = json.loads(ANIDB_CONFIG.read_text())
    except Exception:
        return None

    if not config.get("username") or not config.get("api_password"):
        return None
    if not config.get("client") or not config.get("clientver"):
        return None

    return config


def _anidb_send(sock: socket.socket, command: str) -> tuple[int, str]:
    """Send a UDP command to AniDB, return (response_code, body)."""
    sock.sendto(command.encode("utf-8"), (ANIDB_HOST, ANIDB_PORT))
    data, _ = sock.recvfrom(65536)
    resp = data.decode("utf-8")
    code = int(resp[:3])
    body = resp[4:] if len(resp) > 4 else ""
    return code, body


def _load_anidb_cache() -> dict[str, Any]:
    """Load the AniDB lookup cache. Keys are ed2k hashes."""
    if ANIDB_CACHE.exists():
        try:
            return json.loads(ANIDB_CACHE.read_text())
        except Exception:
            pass
    return {}


def _save_anidb_cache(cache: dict[str, Any]) -> None:
    """Persist the AniDB lookup cache."""
    try:
        ANIDB_CACHE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def anidb_identify(
    mkv_files: list[Path],
) -> dict[Path, tuple[int, int, str]] | None:
    """Identify anime episodes via AniDB ed2k hash + file size.

    Caches results in /config/anidb-cache.json to avoid repeat API calls.
    Returns {path: (season, episode, title)} or None if AniDB is unavailable.
    Regular episodes map to season 1, specials to season 0.
    """
    if not _MD4_AVAILABLE:
        return None

    config = _load_anidb_config()
    if config is None:
        return None

    print("  AniDB: attempting ed2k hash identification...")

    # Compute hashes and check cache
    cache = _load_anidb_cache()
    file_hashes: list[tuple[Path, str, int]] = []  # (path, ed2k, size)
    results: dict[Path, tuple[int, int, str]] = {}

    for mkv in mkv_files:
        ed2k = _ed2k_hash(mkv)
        if not ed2k:
            continue

        cached = cache.get(ed2k)
        if cached is not None:
            if cached:  # cached hit (not a cached miss)
                season, epno, title = cached
                results[mkv] = (season, epno, title)
                print(f"    {mkv.name}: S{season:02d}E{epno:02d} — {title} (cached)")
            else:
                print(f"    {mkv.name}: not in AniDB (cached)")
            continue

        file_hashes.append((mkv, ed2k, mkv.stat().st_size))

    if not file_hashes:
        # Everything was cached
        if results:
            print(f"  AniDB: {len(results)}/{len(mkv_files)} identified (all cached)")
        return results if results else None

    # Need to query — open UDP session
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(10)

    try:
        # Authenticate
        auth_cmd = (
            f"AUTH user={config['username']}&pass={config['api_password']}"
            f"&protover={ANIDB_PROTO_VER}"
            f"&client={config['client']}&clientver={config['clientver']}"
            f"&enc=UTF-8"
        )
        code, body = _anidb_send(sock, auth_cmd)

        if code not in (200, 201):
            print(f"  AniDB: auth failed ({code}): {body.strip()}")
            _notify("AniDB auth failed", f"Code {code}: {body.strip()}", error=True)
            return results if results else None

        session = body.split()[0]
        print(f"  AniDB: authenticated, querying {len(file_hashes)} file(s)...")

        for i, (mkv, ed2k, file_size) in enumerate(file_hashes):
            if i > 0:
                time.sleep(2.0)  # AniDB rate limit: max 1 packet per 2 seconds

            # FILE command: fmask gets aid, amask gets anime name + episode info
            # fmask 0x4000000000 = aid
            # amask 0x00A0C000 = romaji name, english name, epno, ep name
            file_cmd = (
                f"FILE size={file_size}&ed2k={ed2k}"
                f"&fmask=4000000000&amask=00A0C000"
                f"&s={session}"
            )
            code, body = _anidb_send(sock, file_cmd)

            if code == 220:
                # Response: "FILE\n{fid}|{aid}|{romaji}|{english}|{epno}|{ep_name}"
                lines = body.strip().split("\n")
                if len(lines) >= 2:
                    fields = lines[1].split("|")
                    if len(fields) >= 6:
                        anime_name = fields[3] or fields[2]  # prefer english name
                        epno_raw = fields[4]
                        ep_name = fields[5]

                        # Parse epno: "1" = regular, "S1" = special, "C1" = credits, etc.
                        season = 1
                        ep_match = re.match(r"^(\d+)$", epno_raw)
                        if ep_match:
                            epno = int(ep_match.group(1))
                        elif epno_raw.upper().startswith("S") and epno_raw[1:].isdigit():
                            season = 0
                            epno = int(epno_raw[1:])
                        else:
                            print(f"    {mkv.name}: unusual episode format '{epno_raw}', skipping")
                            cache[ed2k] = []  # cache as miss
                            continue

                        results[mkv] = (season, epno, ep_name or anime_name)
                        cache[ed2k] = [season, epno, ep_name or anime_name]
                        print(f"    {mkv.name}: S{season:02d}E{epno:02d} — {ep_name} ({anime_name})")
                    else:
                        print(f"    {mkv.name}: unexpected response format")
            elif code == 320:
                print(f"    {mkv.name}: not in AniDB")
                cache[ed2k] = []  # cache negative result
            elif code == 501:
                print("  AniDB: session expired, stopping")
                break
            elif code in (555, 604):
                # 555 = BANNED, 604 = TIMEOUT - DELAY AND RESUBMIT
                print(f"  AniDB: rate limited ({code}), stopping")
                break
            else:
                print(f"    {mkv.name}: AniDB response ({code}): {body.strip()}")

        # Logout
        try:
            _anidb_send(sock, f"LOGOUT s={session}")
        except Exception:
            pass

    except Exception as e:
        print(f"  AniDB: error: {e}")
    finally:
        sock.close()
        _save_anidb_cache(cache)

    if results:
        print(f"  AniDB: identified {len(results)}/{len(mkv_files)} file(s)")
    else:
        print("  AniDB: no files identified")

    return results if results else None


# ---------------------------------------------------------------------------
# SubDB — hash-based subtitle download for text-matching fallback
# ---------------------------------------------------------------------------

SUBDB_API = "http://api.thesubdb.com/"
SUBDB_USER_AGENT = "SubDB/1.0 (strophalos/1.0; https://github.com/strophalos)"


def _subdb_hash(path: Path) -> str | None:
    """Compute SubDB hash: MD5 of first 64KB + last 64KB."""
    BLOCK_SIZE = 65536

    try:
        file_size = path.stat().st_size
        if file_size < BLOCK_SIZE * 2:
            return None

        md5 = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as f:
            md5.update(f.read(BLOCK_SIZE))
            f.seek(file_size - BLOCK_SIZE)
            md5.update(f.read(BLOCK_SIZE))

        return md5.hexdigest()
    except Exception as e:
        print(f"  SubDB: hash failed for {path.name}: {e}")
        return None


def _subdb_download_subs(file_hash: str) -> str | None:
    """Download English subtitles from SubDB. Returns SRT text or None."""
    import urllib.request

    url = f"{SUBDB_API}?action=download&hash={file_hash}&language=en"
    req = urllib.request.Request(url, headers={"User-Agent": SUBDB_USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace")
    except Exception:
        pass

    return None


def subdb_fetch_subtitles(mkv_path: Path, duration: float) -> list[tuple[float, str]]:
    """Try to fetch subtitles from SubDB for a file.

    Returns (timestamp, text) pairs filtered to first 25% of duration,
    or empty list if unavailable.
    """
    file_hash = _subdb_hash(mkv_path)
    if not file_hash:
        return []

    srt_text = _subdb_download_subs(file_hash)
    if not srt_text:
        return []

    print(f"    SubDB: downloaded subtitles for {mkv_path.name}")

    results = _parse_srt(srt_text)
    cutoff = duration * 0.25
    if cutoff > 0:
        results = [(t, text) for t, text in results if t <= cutoff]

    return results


# ---------------------------------------------------------------------------
# TMDb API
# ---------------------------------------------------------------------------

API_BASE = "https://api.themoviedb.org/3"


def _notify(title: str, body: str, error: bool = False) -> None:
    """Send a notification via notify.sh (no-op if not configured)."""
    try:
        cmd = [NOTIFY_SCRIPT]
        if error:
            cmd.append("--error")
        cmd.extend([title, body])
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass


def _tmdb_get(endpoint: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Make a GET request to TMDb API v3. Returns parsed JSON or None on failure."""
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


def _clean_disc_label(label: str) -> str:
    """Clean a disc label for TMDb search."""
    name = label.replace("_", " ")
    # Strip common disc suffixes
    name = re.sub(r"\s*(S\d+|D\d+|DISC\s*\d+|BDMV|BD|DVD)\s*$", "", name, flags=re.IGNORECASE)
    return name.strip()


def search_series(label: str) -> tuple[int, str] | None:
    """Search TMDb for a TV series. Returns (series_id, name) or None."""
    query = _clean_disc_label(label)
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
            episodes.append(Episode(
                season=season_num,
                episode=ep.get("order", ep.get("episode_number", 0)) + 1,
                title=ep.get("name", ""),
                runtime_seconds=runtime,
            ))

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
            episodes.append(Episode(
                season=s,
                episode=ep.get("episode_number", 0),
                title=ep.get("name", ""),
                runtime_seconds=runtime,
            ))

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
# MKV / subtitle extraction
# ---------------------------------------------------------------------------


def get_mkv_duration(mkv_path: Path) -> float:
    """Get MKV duration in seconds. Tries mkvmerge, falls back to ffprobe."""
    # Try mkvmerge first
    try:
        result = subprocess.run(
            ["mkvmerge", "--identify", "--identification-format", "json", str(mkv_path)],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(result.stdout)
        ns = info.get("container", {}).get("properties", {}).get("duration", 0)
        if ns > 0:
            return ns / 1_000_000_000
    except Exception:
        pass

    # Fallback: ffprobe
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(mkv_path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        pass

    return 0.0


@dataclass
class SubtitleTrack:
    track_id: int
    codec: str
    language: str
    name: str
    is_text: bool


def list_subtitle_tracks(mkv_path: Path) -> list[SubtitleTrack]:
    """List subtitle tracks in an MKV file."""
    try:
        result = subprocess.run(
            ["mkvmerge", "--identify", "--identification-format", "json", str(mkv_path)],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(result.stdout)
    except Exception:
        return []

    tracks: list[SubtitleTrack] = []
    for track in info.get("tracks", []):
        if track.get("type") != "subtitles":
            continue
        props = track.get("properties", {})
        codec = props.get("codec_id", "")
        lang = props.get("language", "und")
        name = (props.get("track_name") or "").lower()
        forced = props.get("forced_track", False)

        # Skip forced/signs-only
        if forced or "sign" in name or "song" in name:
            continue

        is_text = any(t in codec.upper() for t in ("SRT", "ASS", "SSA", "UTF8", "TEXT"))

        tracks.append(SubtitleTrack(
            track_id=track["id"],
            codec=codec,
            language=lang,
            name=name,
            is_text=is_text,
        ))

    return tracks


def select_best_track(tracks: list[SubtitleTrack]) -> SubtitleTrack | None:
    """Pick the best subtitle track: prefer text, then PGS; prefer English.

    For PGS tracks, prefer the FIRST matching track — full dialog subs are
    typically listed before signs/songs/OP lyrics tracks.
    """
    if not tracks:
        return None

    def sort_key(t: SubtitleTrack) -> tuple[int, int, int, int]:
        is_eng = 1 if t.language in ("eng", "en") else 0
        is_text = 1 if t.is_text else 0
        is_und = 1 if t.language == "und" else 0
        # Prefer earlier track ID (lower = first in file = usually full subs)
        earlier = -t.track_id
        return (is_text, is_eng, is_und, earlier)

    return max(tracks, key=sort_key)


def _parse_srt(srt_text: str) -> list[tuple[float, str]]:
    """Parse SRT format into (timestamp_seconds, text) pairs."""
    results: list[tuple[float, str]] = []
    for match in re.finditer(
        r"(\d+):(\d+):([\d,]+)\s*-->.*?\n(.*?)(?:\n\n|\Z)", srt_text, re.DOTALL
    ):
        h, m = int(match.group(1)), int(match.group(2))
        s = float(match.group(3).replace(",", "."))
        timestamp = h * 3600 + m * 60 + s
        line = re.sub(r"<[^>]+>", "", match.group(4)).strip().replace("\n", " ")
        if line:
            results.append((timestamp, line))
    return results


def extract_subtitles(mkv_path: Path, duration: float) -> list[tuple[float, str]]:
    """Extract subtitle text from an MKV via pgsrip (PGS→SRT) or direct extraction (text subs).

    Falls back to SubDB subtitle download when no embedded subs are available.
    Returns (timestamp_seconds, text) pairs filtered to first 25% of duration.
    """
    tracks = list_subtitle_tracks(mkv_path)
    track = select_best_track(tracks)
    if not track:
        # No embedded subs — try SubDB
        return subdb_fetch_subtitles(mkv_path, duration)

    cutoff = duration * 0.25

    with tempfile.TemporaryDirectory() as tmpdir:
        sub_path = Path(tmpdir) / "subs"

        if track.is_text:
            # Text subs: extract directly
            ext = ".srt" if "SRT" in track.codec.upper() else ".ass"
            out = sub_path.with_suffix(ext)
            print(f"    subtitle: {track.codec} ({track.language}) [text]")
            try:
                subprocess.run(
                    ["mkvextract", "tracks", str(mkv_path), f"{track.track_id}:{out}"],
                    capture_output=True, timeout=30,
                )
            except Exception:
                return []
            if not out.exists():
                return []

            text = out.read_text(errors="replace")
            if "ASS" in track.codec.upper() or "SSA" in track.codec.upper():
                results: list[tuple[float, str]] = []
                for match in re.finditer(
                    r"Dialogue:\s*\d+,(\d+):(\d+):([\d.]+),.*?,.*?,.*?,.*?,.*?,.*?,(.*)", text
                ):
                    h, m, s2 = int(match.group(1)), int(match.group(2)), float(match.group(3))
                    ts = h * 3600 + m * 60 + s2
                    line = re.sub(r"\{[^}]*\}", "", match.group(4)).strip()
                    if line:
                        results.append((ts, line))
            else:
                results = _parse_srt(text)
        else:
            # PGS subs: extract .sup then OCR via pgsrip
            sup_path = sub_path.with_suffix(".sup")
            print(f"    subtitle: {track.codec} ({track.language}) [PGS→pgsrip]")
            try:
                subprocess.run(
                    ["mkvextract", "tracks", str(mkv_path), f"{track.track_id}:{sup_path}"],
                    capture_output=True, timeout=60,
                )
            except Exception:
                return []
            if not sup_path.exists() or sup_path.stat().st_size == 0:
                return []

            # Run pgsrip to produce .srt
            try:
                subprocess.run(
                    ["python3", "-m", "pgsrip", str(sup_path)],
                    capture_output=True, timeout=300,
                )
            except Exception:
                return []

            srt_path = sup_path.with_suffix(".srt")
            if not srt_path.exists():
                return []

            results = _parse_srt(srt_path.read_text(errors="replace"))

    if cutoff > 0:
        results = [(t, text) for t, text in results if t <= cutoff]

    # If embedded subs yielded nothing, try SubDB
    if not results:
        return subdb_fetch_subtitles(mkv_path, duration)

    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Split into lowercase alphanumeric words."""
    return re.findall(r"[a-z0-9]+", text.lower())


def compute_word_weights(episodes: list[Episode]) -> dict[str, float]:
    """Compute per-word scoring weights. Common words get reduced weight."""
    n = len(episodes)
    if n == 0:
        return {}

    doc_freq: Counter[str] = Counter()
    for ep in episodes:
        words = set(tokenize(ep.title))
        for w in words:
            doc_freq[w] += 1

    weights: dict[str, float] = {}
    for word, count in doc_freq.items():
        weights[word] = 0.1 if count / n > 0.5 else 1.0
    return weights


def position_weight(t: float, duration: float) -> float:
    """Weight by position in episode. Smooth taper from 1.5x at start to 1.0x by 20%."""
    if duration <= 0:
        return 1.0
    frac = t / duration
    if frac <= 0.20:
        return 1.0 + 0.5 * (1.0 - frac / 0.20)
    if frac >= 0.75:
        return 0.3
    return 1.0


def score_match(
    episode: Episode,
    subtitle_texts: list[tuple[float, str]],
    word_weights: dict[str, float],
    file_duration: float,
) -> float:
    """Score how well subtitle text matches an episode title.

    Each title word is scored at most once, using its best position weight
    across all subtitle cues. This prevents common words that appear in many
    cues from inflating the score.
    """
    title_words = tokenize(episode.title)
    if not title_words:
        return 0.0

    # Discriminating words = the ones that actually distinguish episodes
    disc_words = [w for w in title_words if word_weights.get(w, 1.0) > 0.5]

    # Phrase match: if all discriminating words appear in a single subtitle cue,
    # that's likely the title card. This is a very strong signal.
    phrase_bonus = 0.0
    if disc_words:
        for timestamp, text in subtitle_texts:
            cue_words = set(tokenize(text))
            if all(w in cue_words for w in disc_words):
                pw = position_weight(timestamp, file_duration)
                phrase_bonus = max(phrase_bonus, 5.0 * pw)
                break  # Best phrase match found

    # Per-word scoring: each title word scored once at its best position
    best_pw: dict[str, float] = {}
    for timestamp, text in subtitle_texts:
        sub_words = set(tokenize(text))
        pw = position_weight(timestamp, file_duration)
        for word in title_words:
            if word in sub_words:
                best_pw[word] = max(best_pw.get(word, 0.0), pw)

    word_score = 0.0
    for word in title_words:
        if word in best_pw:
            weight = word_weights.get(word, 1.0)
            word_score += weight * best_pw[word]

    return word_score + phrase_bonus


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def hungarian_assignment(score_matrix: list[list[float]]) -> list[tuple[int, int, float]]:
    """Optimal assignment maximizing total score. Returns (row, col, score) triples.

    Pure Python implementation of the Hungarian algorithm for rectangular matrices.
    Pads to square with zeros, then uses the Munkres method.
    """
    n_rows = len(score_matrix)
    if n_rows == 0:
        return []
    n_cols = len(score_matrix[0])
    n = max(n_rows, n_cols)

    # Negate for minimization and pad to square
    cost: list[list[float]] = []
    max_val = max(max(row) for row in score_matrix) if score_matrix else 0
    for i in range(n):
        row: list[float] = []
        for j in range(n):
            if i < n_rows and j < n_cols:
                row.append(max_val - score_matrix[i][j])
            else:
                row.append(0.0)
        cost.append(row)

    # Munkres algorithm
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (n + 1)
        used = [False] * (n + 1)

        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = -1

            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j

            if j1 == -1:
                break

            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta

            j0 = j1
            if p[j0] == 0:
                break

        while j0 != 0:
            p[j0] = p[way[j0]]
            j0 = way[j0]

    results: list[tuple[int, int, float]] = []
    for j in range(1, n + 1):
        i = p[j] - 1
        if i < n_rows and j - 1 < n_cols:
            results.append((i, j - 1, score_matrix[i][j - 1]))

    return results


def _build_score_matrix(
    files: list[RippedFile],
    window: list[Episode],
    word_weights: dict[str, float],
) -> list[list[float]]:
    """Build the N×M score matrix for files against a window of episodes."""
    matrix: list[list[float]] = []
    for f in files:
        row: list[float] = []
        for ep in window:
            dur_score = 0.0
            if ep.runtime_seconds > 0:
                dur_score = max(0, 1.0 - abs(f.duration_seconds - ep.runtime_seconds) / ep.runtime_seconds) * 2.0
            sub_score = score_match(ep, f.subtitle_texts, word_weights, f.duration_seconds)
            row.append(dur_score + sub_score)
        matrix.append(row)
    return matrix


def find_best_window(
    files: list[RippedFile],
    episodes: list[Episode],
    word_weights: dict[str, float],
) -> tuple[int, float]:
    """Slide a window of len(files) across episodes, return (start_idx, best_score).

    For each window position, compute the optimal assignment score (Hungarian)
    so that a few noisy matches don't drag the window to the wrong position.
    """
    n = len(files)
    m = len(episodes)
    if n == 0 or m == 0 or n > m:
        return 0, 0.0

    best_start = 0
    best_score = -1.0

    for start in range(m - n + 1):
        window = episodes[start : start + n]
        matrix = _build_score_matrix(files, window, word_weights)
        assignments = hungarian_assignment(matrix)
        total = sum(matrix[i][j] for i, j, _ in assignments if i < n and j < n)

        if total > best_score:
            best_score = total
            best_start = start

    return best_start, best_score


def assign_episodes(
    files: list[RippedFile],
    window_episodes: list[Episode],
    word_weights: dict[str, float],
) -> list[tuple[int, int, float]]:
    """Assign files to episodes within a window. Returns (file_idx, ep_idx, score)."""
    n = len(files)
    m = len(window_episodes)

    matrix = _build_score_matrix(files, window_episodes, word_weights)

    # Try forward ordering: bonus for maintaining disc order = episode order
    forward_matrix = [row[:] for row in matrix]
    for i in range(n):
        if i < m:
            forward_matrix[i][i] += 0.5  # Small ordering bonus

    # Try reverse ordering
    reverse_matrix = [row[:] for row in matrix]
    for i in range(n):
        rev_j = m - 1 - i
        if 0 <= rev_j < m:
            reverse_matrix[i][rev_j] += 0.5

    # Solve all three, pick best total
    candidates = [
        ("forward", hungarian_assignment(forward_matrix)),
        ("reverse", hungarian_assignment(reverse_matrix)),
        ("unordered", hungarian_assignment(matrix)),
    ]

    best_name = ""
    best_result: list[tuple[int, int, float]] = []
    best_total = -1.0

    for name, assignments in candidates:
        total = sum(matrix[i][j] for i, j, _ in assignments if i < n and j < m)
        if total > best_total:
            best_total = total
            best_result = [(i, j, matrix[i][j]) for i, j, _ in assignments if i < n and j < m]
            best_name = name

    if best_name:
        print(f"  Ordering: {best_name} (score={best_total:.2f})")

    return best_result


# ---------------------------------------------------------------------------
# Renaming
# ---------------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters."""
    name = name.replace(":", " -")
    name = re.sub(r'[?*<>|"\\]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name





# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Episode identification for TV disc rips")
    parser.add_argument("--dir", required=True, help="Archive disc directory containing ripped MKV files")
    parser.add_argument("--label", required=True, help="Disc label for TMDb search")
    parser.add_argument("--library", default="/media", help="Library root for hard-linked output")
    parser.add_argument("--dry-run", action="store_true", help="Show proposed links without executing")
    parser.add_argument("--min-score", type=float, default=0.5, help="Minimum score for matching")
    parser.add_argument("--verbose", action="store_true", help="Show OCR text and detailed scoring")
    parser.add_argument(
        "--no-assume-order", action="store_true",
        help="Don't assume discs are inserted in order (disable forward-order fallback)",
    )
    args = parser.parse_args()

    # ASSUME_DISC_ORDER env var (default true) — CLI flag overrides
    assume_order = os.environ.get("ASSUME_DISC_ORDER", "1").lower() not in ("0", "false", "no")
    if args.no_assume_order:
        assume_order = False

    out_dir = Path(args.dir)
    if not out_dir.is_dir():
        print(f"Directory not found: {out_dir}")
        return

    # Find MKV files
    # Only process makemkv output files (*_tNN.mkv), not already-renamed episodes
    mkv_files = sorted(out_dir.glob("*_t[0-9][0-9].mkv"))
    if not mkv_files:
        print("No MKV files found")
        return

    print(f"Found {len(mkv_files)} MKV file(s)")

    # -----------------------------------------------------------------------
    # Pipeline: accumulate MatchResults, rename + notify at end
    # -----------------------------------------------------------------------
    series_name = _clean_disc_label(args.label)
    library_dir = Path(args.library)

    # Build RippedFile objects with durations
    files: list[RippedFile] = []
    for mkv in mkv_files:
        dur = get_mkv_duration(mkv)
        files.append(RippedFile(path=mkv, duration_seconds=dur))
        print(f"  {mkv.name}: {dur:.0f}s")

    results: dict[Path, MatchResult] = {}
    remaining = list(files)

    # --- Layer 0: Hash-based identification (OpenSubtitles + AniDB) ---
    hash_results = opensubtitles_identify(mkv_files)
    if hash_results:
        for path, (season, episode, title) in hash_results.items():
            rf = next((f for f in remaining if f.path == path), None)
            if rf:
                ep = Episode(season=season, episode=episode, title=title, runtime_seconds=0)
                results[path] = MatchResult(file=rf, episode=ep, method="opensubtitles hash")
                remaining = [f for f in remaining if f.path != path]
        print(f"  OpenSubtitles: {len(hash_results)}/{len(files)} identified via hash")

    # AniDB: try remaining files (anime identification)
    if remaining:
        remaining_paths = [f.path for f in remaining]
        anidb_results = anidb_identify(remaining_paths)
        if anidb_results:
            for path, (season, episode, title) in anidb_results.items():
                rf = next((f for f in remaining if f.path == path), None)
                if rf:
                    ep = Episode(season=season, episode=episode, title=title, runtime_seconds=0)
                    results[path] = MatchResult(file=rf, episode=ep, method="anidb hash")
                    remaining = [f for f in remaining if f.path != path]
            print(f"  AniDB: {len(anidb_results)}/{len(files)} identified via ed2k hash")

    if not remaining:
        print("  All files identified via hash lookup")
    else:
        # --- Layer 1+: TMDb + duration + subtitles ---
        episodes, specials, group_name, tmdb_name = fetch_all_episodes(args.label)
        if tmdb_name:
            series_name = tmdb_name
        if not episodes and not tmdb_name:
            msg = f"No TMDb match for '{series_name}'. Add the series at https://www.themoviedb.org and re-run:\n"
            msg += f"  docker exec strophalos identify-episodes.py --dir {args.dir} --label {args.label}"
            print(f"  {msg}")
            _notify(f"{series_name}: identification failed", msg, error=True)
        elif not episodes:
            msg = f"TMDb matched '{series_name}' but no episodes found. Check the TMDb entry has seasons/episodes."
            print(f"  {msg}")
            _notify(f"{series_name}: no episodes on TMDb", msg, error=True)
        if episodes:
            n_seasons = len({ep.season for ep in episodes})
            print(f"  {len(episodes)} episodes across {n_seasons} season(s)")

            # Exclude episodes already matched by hash or already in directory
            matched_ep_keys = {(r.episode.season, r.episode.episode) for r in results.values()}
            # Scan library for already-linked episodes
            existing_eps: set[tuple[int, int]] = set()
            lib_series_dir = library_dir / "tv" / sanitize_filename(series_name)
            if lib_series_dir.exists():
                for existing in lib_series_dir.glob("S[0-9][0-9]E[0-9][0-9]*.mkv"):
                    m = re.match(r"S(\d+)E(\d+)", existing.name)
                    if m:
                        existing_eps.add((int(m.group(1)), int(m.group(2))))
            if existing_eps:
                print(f"  {len(existing_eps)} already-linked episode(s) in library")

            exclude = matched_ep_keys | existing_eps
            episodes = [ep for ep in episodes if (ep.season, ep.episode) not in exclude]

            if episodes and remaining:
                # Duration filter
                need_subs = False
                for f in remaining:
                    candidates = [
                        ep for ep in episodes
                        if ep.runtime_seconds <= 0
                        or abs(f.duration_seconds - ep.runtime_seconds) / max(ep.runtime_seconds, 1) <= 0.05
                    ]
                    if len(candidates) != 1:
                        need_subs = True
                        break

                if need_subs:
                    print("  Duration-only matching insufficient, extracting subtitles...")
                    def _process_file(f: RippedFile) -> tuple[RippedFile, int]:
                        f.subtitle_texts = extract_subtitles(f.path, f.duration_seconds)
                        return f, len(f.subtitle_texts)

                    with ThreadPoolExecutor(max_workers=min(4, len(remaining))) as pool:
                        futures = {pool.submit(_process_file, f): f for f in remaining}
                        for future in as_completed(futures):
                            f, n_cues = future.result()
                            print(f"  {f.path.name}: {n_cues} subtitle cue(s)")

                    # Deduplicate common cues (OP/ED)
                    if len(remaining) > 1:
                        cue_counts: Counter[str] = Counter()
                        for f in remaining:
                            seen: set[str] = set()
                            for _, text in f.subtitle_texts:
                                normalized = text.strip().lower()
                                if normalized not in seen:
                                    cue_counts[normalized] += 1
                                    seen.add(normalized)
                        threshold = len(remaining) // 2
                        common_cues = {t for t, c in cue_counts.items() if c > threshold}
                        if common_cues:
                            for f in remaining:
                                f.subtitle_texts = [
                                    (t, text) for t, text in f.subtitle_texts
                                    if text.strip().lower() not in common_cues
                                ]
                else:
                    print("  Duration matching sufficient")

                # Window search + assignment
                word_weights = compute_word_weights(episodes)
                n = len(remaining)

                print("  Finding best episode window...")
                window_start, window_score = find_best_window(remaining, episodes, word_weights)
                window = episodes[window_start : window_start + n]

                if window:
                    print(f"  Best window: {window[0].code}–{window[-1].code} (score={window_score:.2f})")

                    # Check if forward-order fallback is needed
                    use_forward = False
                    scores_by_window: list[float] = []
                    for start in range(max(1, len(episodes) - n + 1)):
                        w = episodes[start : start + n]
                        if len(w) == n:
                            mx = _build_score_matrix(remaining, w, word_weights)
                            a = hungarian_assignment(mx)
                            scores_by_window.append(sum(mx[i][j] for i, j, _ in a if i < n and j < n))

                    if scores_by_window:
                        best = max(scores_by_window)
                        second = sorted(scores_by_window, reverse=True)[1] if len(scores_by_window) > 1 else 0
                        margin = (best - second) / best if best > 0 else 0
                        if margin < 0.15 and assume_order and existing_eps:
                            # We have prior context — continue from where we left off
                            window = episodes[:n]
                            print(f"  Scores indistinct (margin={margin:.0%}), forward order: {window[0].code}–{window[-1].code}")
                            use_forward = True
                        elif margin < 0.15:
                            # No prior context and no signal — refuse to guess
                            print(f"  Scores indistinct (margin={margin:.0%}), no prior context — cannot identify")
                            _notify(
                                f"{series_name}: identification failed",
                                f"No subtitle/hash/duration signal and no prior disc context.\n"
                                f"{len(remaining)} file(s) in archive, manual identification needed.",
                                error=True,
                            )
                            remaining = []  # Don't assign anything

                    if use_forward:
                        for i, f in enumerate(remaining):
                            if i < len(window):
                                results[f.path] = MatchResult(
                                    file=f, episode=window[i], method="forward order",
                                )
                    else:
                        assignments = assign_episodes(remaining, window, word_weights)
                        method = "subtitle match" if need_subs else "duration"
                        for file_idx, ep_idx, score in assignments:
                            if score >= args.min_score:
                                f = remaining[file_idx]
                                results[f.path] = MatchResult(
                                    file=f, episode=window[ep_idx],
                                    method=method, score=score,
                                )

    # -----------------------------------------------------------------------
    # Hard-link matched files to library
    # -----------------------------------------------------------------------
    unmatched_names: list[str] = []
    matched_manifest: dict[str, dict[str, Any]] = {}

    lib_series_dir = library_dir / "tv" / sanitize_filename(series_name)

    for f in files:
        if f.path not in results:
            unmatched_names.append(f.path.name)
            continue

        r = results[f.path]
        title_safe = sanitize_filename(r.episode.title)
        new_name = f"{r.episode.code} - {title_safe}.mkv" if title_safe else f"{r.episode.code}.mkv"
        link_path = lib_series_dir / new_name

        if link_path.exists():
            if link_path.stat().st_ino == f.path.stat().st_ino:
                print(f"  skip (already linked): {f.path.name} → {new_name}")
                continue
            # Different file at same name — flag conflict
            print(f"  conflict: {new_name} exists with different inode")
            _notify(
                f"🚫🔗 {series_name} {r.episode.code}: link conflict",
                f"Library already has a different copy:\n{link_path}\n\nNew rip: {f.path}\nResolve manually.",
                error=True,
            )
            continue

        action = "would link" if args.dry_run else "link"
        rel_path = link_path.relative_to(library_dir) if library_dir in link_path.parents else new_name
        print(f"  {action}: {f.path.name} → {rel_path}  [{r.method}]")

        if not args.dry_run:
            lib_series_dir.mkdir(parents=True, exist_ok=True)
            os.link(f.path, link_path)

        matched_manifest[new_name] = {
            "original": str(f.path),
            "season": r.episode.season,
            "episode": r.episode.episode,
            "title": r.episode.title,
            "method": r.method,
            "score": round(r.score, 2),
        }

    if unmatched_names:
        print(f"  Unmatched: {unmatched_names}")

    # -----------------------------------------------------------------------
    # Write manifest
    # -----------------------------------------------------------------------
    if not args.dry_run and matched_manifest:
        manifest = {
            "series": series_name,
            "matched": matched_manifest,
            "unmatched": unmatched_names,
            "timestamp": datetime.now().isoformat(),
        }
        manifest_path = out_dir / ".episode-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  Manifest written: {manifest_path}")

    # -----------------------------------------------------------------------
    # Notification
    # -----------------------------------------------------------------------
    if not matched_manifest and not unmatched_names:
        return

    # Group results by method
    by_method: dict[str, list[MatchResult]] = {}
    for r in results.values():
        by_method.setdefault(r.method, []).append(r)

    # Build episode range strings (detect contiguous runs)
    def _format_ranges(match_results: list[MatchResult]) -> str:
        eps = sorted((r.episode.season, r.episode.episode) for r in match_results)
        if not eps:
            return ""
        ranges: list[str] = []
        run_start = run_end = eps[0]
        for s, e in eps[1:]:
            prev_s, prev_e = run_end
            if s == prev_s and e == prev_e + 1:
                run_end = (s, e)
            else:
                ranges.append(
                    f"S{run_start[0]:02d}E{run_start[1]:02d}–E{run_end[1]:02d}"
                    if run_start[0] == run_end[0] and run_start != run_end
                    else f"S{run_start[0]:02d}E{run_start[1]:02d}"
                    if run_start == run_end
                    else f"S{run_start[0]:02d}E{run_start[1]:02d}–S{run_end[0]:02d}E{run_end[1]:02d}"
                )
                run_start = run_end = (s, e)
        ranges.append(
            f"S{run_start[0]:02d}E{run_start[1]:02d}–E{run_end[1]:02d}"
            if run_start[0] == run_end[0] and run_start != run_end
            else f"S{run_start[0]:02d}E{run_start[1]:02d}"
            if run_start == run_end
            else f"S{run_start[0]:02d}E{run_start[1]:02d}–S{run_end[0]:02d}E{run_end[1]:02d}"
        )
        return ", ".join(ranges)

    lines: list[str] = []
    all_range = _format_ranges(list(results.values()))
    lines.append(all_range)
    lines.append("")
    for method, mrs in sorted(by_method.items()):
        r_str = _format_ranges(mrs)
        lines.append(f"{method}: {r_str}")
    lines.append("")
    lines.append(f"{len(files)} title(s) → {len(matched_manifest)} linked to library")
    if unmatched_names:
        lines.append(f"{len(unmatched_names)} unmapped file(s) in archive")

    body = "\n".join(lines)
    title = f"{series_name}: episodes linked"
    print(f"\n{title}\n{body}")

    if unmatched_names:
        _notify(title, body, error=True)
    else:
        _notify(title, body)


if __name__ == "__main__":
    main()
