"""AniDB UDP API client — ed2k hash-based anime identification.

Custom implementation using the ed2k PyPI library for hashing and direct UDP
socket for the AniDB API. Rate-limited (2s first 5 packets, 4s after) with
JSON file cache of both hits and misses.
"""

from __future__ import annotations

import json
import re
import socket
import time
from pathlib import Path
from typing import Any

ANIDB_CONFIG = Path("/config/anidb.json")
ANIDB_CACHE = Path("/config/anidb-cache.json")
ANIDB_HOST = "api.anidb.net"
ANIDB_PORT = 9000
ANIDB_PROTO_VER = 3
ANIDB_CLIENT = "strophalos"
ANIDB_CLIENTVER = 1


def _load_config() -> dict[str, Any] | None:
    """Load AniDB credentials from /config/anidb.json."""
    if not ANIDB_CONFIG.exists():
        return None
    try:
        config = json.loads(ANIDB_CONFIG.read_text())
    except Exception:
        return None

    if not config.get("username") or not config.get("password"):
        return None
    return config


def _ed2k_hash(path: Path) -> tuple[str, int] | None:
    """Compute ed2k hash and file size. Returns (hash_hex, size) or None."""
    try:
        from ed2k import ed2k_hash
    except ImportError:
        return None

    try:
        with open(path, "rb") as f:
            size, h = ed2k_hash(f)
        return h, size
    except Exception:
        return None


def _load_cache() -> dict[str, Any]:
    """Load the AniDB lookup cache."""
    if ANIDB_CACHE.exists():
        try:
            return json.loads(ANIDB_CACHE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, Any]) -> None:
    """Save the AniDB lookup cache."""
    try:
        ANIDB_CACHE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _send(sock: socket.socket, cmd: str) -> tuple[int, str]:
    """Send a UDP command and receive the response."""
    sock.sendto(cmd.encode("utf-8"), (ANIDB_HOST, ANIDB_PORT))
    data, _ = sock.recvfrom(65536)
    resp = data.decode("utf-8")
    return int(resp[:3]), resp[4:] if len(resp) > 4 else ""


def identify(mkv_files: list[Path]) -> dict[Path, tuple[int, int, str]] | None:
    """Identify anime episodes via AniDB ed2k hash + file size.

    Returns {path: (season, episode, title)} or None if AniDB is unavailable.
    """
    config = _load_config()
    if config is None:
        return None

    try:
        import importlib.util

        if importlib.util.find_spec("ed2k") is None:
            raise ImportError("ed2k not installed")
    except ImportError:
        print("  AniDB: ed2k library not installed")
        return None

    print("  AniDB: attempting ed2k hash identification...")

    cache = _load_cache()
    results: dict[Path, tuple[int, int, str]] = {}
    need_lookup: list[tuple[Path, str, int]] = []

    # First pass: compute hashes and check cache
    for mkv in mkv_files:
        e2k = _ed2k_hash(mkv)
        if not e2k:
            print(f"    {mkv.name}: ed2k hash failed, skipping")
            continue

        ed2k_hex, file_size = e2k
        cache_key = f"{ed2k_hex}:{file_size}"

        if cache_key in cache:
            cached = cache[cache_key]
            if cached is None:
                print(f"    {mkv.name}: not in AniDB (cached)")
            else:
                results[mkv] = (cached["season"], cached["episode"], cached["title"])
                print(f"    {mkv.name}: S{cached['season']:02d}E{cached['episode']:02d} — {cached['title']} (cached)")
        else:
            need_lookup.append((mkv, ed2k_hex, file_size))

    if not need_lookup:
        if results:
            print(f"  AniDB: {len(results)}/{len(mkv_files)} identified (all cached)")
        return results if results else None

    # UDP lookup for uncached files
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(10)
    packet_count = 0

    try:
        code, body = _send(
            sock,
            f"AUTH user={config['username']}&pass={config['password']}"
            f"&protover={ANIDB_PROTO_VER}&client={ANIDB_CLIENT}&clientver={ANIDB_CLIENTVER}&enc=UTF-8",
        )
        packet_count += 1

        if code not in (200, 201):
            print(f"  AniDB: auth failed ({code}): {body.strip()}")
            return results if results else None

        session = body.split()[0]

        for mkv, ed2k_hex, file_size in need_lookup:
            cache_key = f"{ed2k_hex}:{file_size}"

            # Rate limit
            delay = 2.0 if packet_count < 5 else 4.0
            time.sleep(delay)

            # FILE command: fmask=4000000000 amask=00A0C000
            # Returns: anime romaji name, anime english name, episode number, episode name
            cmd = f"FILE size={file_size}&ed2k={ed2k_hex}&fmask=4000000000&amask=00A0C000&s={session}"
            code, body = _send(sock, cmd)
            packet_count += 1

            if code == 220:
                lines = body.strip().split("\n")
                if len(lines) >= 2:
                    fields = lines[1].split("|")
                    if len(fields) >= 6:
                        anime_name = fields[3] or fields[2]
                        epno_raw = fields[4]
                        ep_name = fields[5]

                        # Parse episode number
                        season = 1
                        ep_match = re.match(r"^(\d+)$", epno_raw)
                        if ep_match:
                            epno = int(ep_match.group(1))
                        elif epno_raw.upper().startswith("S") and epno_raw[1:].isdigit():
                            season = 0
                            epno = int(epno_raw[1:])
                        else:
                            print(f"    {mkv.name}: unusual episode format '{epno_raw}', skipping")
                            cache[cache_key] = None
                            continue

                        results[mkv] = (season, epno, ep_name or anime_name)
                        cache[cache_key] = {"season": season, "episode": epno, "title": ep_name or anime_name}
                        print(f"    {mkv.name}: S{season:02d}E{epno:02d} — {ep_name} ({anime_name})")
                    else:
                        print(f"    {mkv.name}: unexpected response format")
                        cache[cache_key] = None
            elif code == 320:
                print(f"    {mkv.name}: not in AniDB")
                cache[cache_key] = None
            elif code in (555, 604):
                print(f"    {mkv.name}: rate limited/banned ({code}), stopping")
                break
            else:
                print(f"    {mkv.name}: AniDB response ({code}): {body.strip()}")

        # Logout
        try:
            time.sleep(2.0)
            _send(sock, f"LOGOUT s={session}")
        except Exception:
            pass

    except Exception as e:
        print(f"  AniDB: error: {e}")
    finally:
        sock.close()
        _save_cache(cache)

    if results:
        print(f"  AniDB: identified {len(results)}/{len(mkv_files)} file(s)")
    else:
        print("  AniDB: no files identified")

    return results if results else None
