#!/usr/bin/env python3
"""Validation script for episode identification methods.

Tests AniDB (adbb), SubDB, and OpenSubtitles hash computation against
existing ripped MKV files. Does NOT modify any files.

Usage:
    python3 test-identification.py
    ANIDB_USER=hecat3 ANIDB_PASS=xxx python3 test-identification.py
"""

import hashlib
import json
import os
import re
import struct
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Test files
# ---------------------------------------------------------------------------

SPICE_WOLF_DIR = Path("/mnt/pool/library/archive/tv/rips/bd/SPICE_AND_WOLF/disc1")
MARIO_DIR = Path("/mnt/pool/library/archive/tv/rips/dvd/SuperMarioBros/disc1")

# Expected results from existing manifests (disc1 Spice & Wolf = S01E01-E09)
SPICE_WOLF_EXPECTED = {
    "Spice and Wolf_t00.mkv": (1, 1, "Wolf and Best Clothes"),
    "Spice and Wolf_t01.mkv": (1, 2, "Wolf and Distant Past"),
    "Spice and Wolf_t02.mkv": (1, 3, "Wolf and Business Talent"),
}


def banner(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Hash computation tests
# ---------------------------------------------------------------------------


def opensubtitles_hash(path: Path) -> str | None:
    """OpenSubtitles hash: file_size + first 64KB + last 64KB as 64-bit ints."""
    BLOCK = 65536
    try:
        size = path.stat().st_size
        if size < BLOCK * 2:
            return None
        h = size
        fmt = "<" + str(BLOCK // 8) + "Q"
        with open(path, "rb") as f:
            h += sum(struct.unpack(fmt, f.read(BLOCK)))
            f.seek(max(0, size - BLOCK))
            h += sum(struct.unpack(fmt, f.read(BLOCK)))
        return f"{h & 0xFFFFFFFFFFFFFFFF:016x}"
    except Exception as e:
        return f"ERROR: {e}"


def subdb_hash(path: Path) -> str | None:
    """SubDB hash: MD5 of first 64KB + last 64KB."""
    BLOCK = 65536
    try:
        size = path.stat().st_size
        if size < BLOCK * 2:
            return None
        md5 = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as f:
            md5.update(f.read(BLOCK))
            f.seek(size - BLOCK)
            md5.update(f.read(BLOCK))
        return md5.hexdigest()
    except Exception as e:
        return f"ERROR: {e}"


def ed2k_hash_file(path: Path) -> tuple[str, int] | None:
    """ed2k hash via the ed2k library. Returns (hash, size) or None."""
    try:
        from ed2k import ed2k_hash
    except ImportError:
        return None

    try:
        with open(path, "rb") as f:
            size, h = ed2k_hash(f)
        return h, size
    except Exception as e:
        return None


def test_hash_computation(files: list[Path], label: str) -> dict[Path, dict]:
    """Compute all three hash types for a list of files."""
    banner(f"Hash Computation: {label}")
    results = {}

    for f in files:
        size_gb = f.stat().st_size / (1024**3)
        print(f"  {f.name} ({size_gb:.1f} GB)")

        t0 = time.monotonic()
        os_hash = opensubtitles_hash(f)
        t_os = time.monotonic() - t0

        t0 = time.monotonic()
        sd_hash = subdb_hash(f)
        t_sd = time.monotonic() - t0

        t0 = time.monotonic()
        e2k_result = ed2k_hash_file(f)
        t_e2k = time.monotonic() - t0
        e2k = f"{e2k_result[0]} (size={e2k_result[1]})" if e2k_result else "ERROR"

        print(f"    OpenSubtitles: {os_hash}  ({t_os:.1f}s)")
        print(f"    SubDB:         {sd_hash}  ({t_sd:.1f}s)")
        print(f"    ed2k:          {e2k}  ({t_e2k:.1f}s)")

        results[f] = {"opensubtitles": os_hash, "subdb": sd_hash, "ed2k": e2k_result}

    return results


# ---------------------------------------------------------------------------
# SubDB API test
# ---------------------------------------------------------------------------


def test_subdb_lookup(files: list[Path], label: str) -> None:
    """Try SubDB subtitle lookup for each file."""
    import urllib.request

    banner(f"SubDB Subtitle Lookup: {label}")

    API = "http://api.thesubdb.com/"
    UA = "SubDB/1.0 (strophalos/1.0; https://github.com/strophalos)"

    for f in files:
        sd = subdb_hash(f)
        if not sd:
            print(f"  {f.name}: hash failed, skipping")
            continue

        # Search for available languages
        url = f"{API}?action=search&hash={sd}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                langs = resp.read().decode()
                print(f"  {f.name}: FOUND — languages: {langs}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  {f.name}: not in SubDB (404)")
            else:
                print(f"  {f.name}: HTTP {e.code}")
        except Exception as e:
            print(f"  {f.name}: error — {e}")


# ---------------------------------------------------------------------------
# AniDB / adbb test
# ---------------------------------------------------------------------------


def test_anidb_lookup(files: list[Path], label: str, expected: dict | None = None) -> None:
    """Test AniDB lookup via direct UDP API."""
    import socket

    banner(f"AniDB Lookup (UDP): {label}")

    ANIDB_HOST = "api.anidb.net"
    ANIDB_PORT = 9000

    # Get credentials from env or config
    user = os.environ.get("ANIDB_USER", "")
    passwd = os.environ.get("ANIDB_PASS", "")

    if not user or not passwd:
        for cfg_path in (Path("/config/anidb.json"), Path("anidb.json")):
            if cfg_path.exists():
                try:
                    cfg = json.loads(cfg_path.read_text())
                    user = cfg.get("username", "")
                    passwd = cfg.get("password", "")
                except Exception:
                    pass
                if user and passwd:
                    break

    if not user or not passwd:
        print("  SKIP: no AniDB credentials")
        print("  Set ANIDB_USER and ANIDB_PASS env vars, or create anidb.json")
        return

    def send(sock, cmd):
        sock.sendto(cmd.encode("utf-8"), (ANIDB_HOST, ANIDB_PORT))
        data, _ = sock.recvfrom(65536)
        resp = data.decode("utf-8")
        return int(resp[:3]), resp[4:] if len(resp) > 4 else ""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(10)
    packet_count = 0

    try:
        # Auth
        code, body = send(sock, f"AUTH user={user}&pass={passwd}&protover=3&client=strophalos&clientver=1&enc=UTF-8")
        packet_count += 1
        if code not in (200, 201):
            print(f"  AUTH FAILED ({code}): {body.strip()}")
            return

        session = body.split()[0]
        print(f"  Authenticated as {user}")

        for f in files:
            # Compute ed2k hash
            e2k_result = ed2k_hash_file(f)
            if not e2k_result:
                print(f"\n  {f.name}: ed2k hash failed, skipping")
                continue

            ed2k, file_size = e2k_result
            size_gb = file_size / (1024**3)
            print(f"\n  {f.name} ({size_gb:.1f} GB):")
            print(f"    ed2k: {ed2k}  size: {file_size}")

            # Rate limit: 2s first 5 packets, 4s after
            delay = 2.0 if packet_count < 5 else 4.0
            time.sleep(delay)

            # FILE command
            cmd = f"FILE size={file_size}&ed2k={ed2k}&fmask=4000000000&amask=00A0C000&s={session}"
            code, body = send(sock, cmd)
            packet_count += 1

            if code == 220:
                lines = body.strip().split("\n")
                if len(lines) >= 2:
                    fields = lines[1].split("|")
                    if len(fields) >= 6:
                        anime_name = fields[3] or fields[2]
                        epno_raw = fields[4]
                        ep_name = fields[5]
                        print(f"    FOUND: {anime_name}")
                        print(f"    Episode: {epno_raw} — {ep_name}")

                        # Compare against expected
                        if expected and f.name in expected:
                            exp_s, exp_e, exp_t = expected[f.name]
                            ep_match = re.match(r"^(\d+)$", epno_raw)
                            if ep_match:
                                got_ep = int(ep_match.group(1))
                                status = "MATCH" if got_ep == exp_e else f"MISMATCH (got E{got_ep})"
                                print(f"    Expected: S{exp_s:02d}E{exp_e:02d} — {exp_t}")
                                print(f"    Status:   {status}")
                    else:
                        print(f"    Unexpected response format: {lines[1]}")
            elif code == 320:
                print(f"    NOT IN ANIDB (320)")
            elif code in (555, 604):
                print(f"    RATE LIMITED/BANNED ({code}), stopping")
                break
            else:
                print(f"    Response ({code}): {body.strip()}")

        # Logout
        try:
            time.sleep(2.0)
            send(sock, f"LOGOUT s={session}")
        except Exception:
            pass

    except Exception as e:
        print(f"  ERROR: {e}")
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# pydvdid-m import test
# ---------------------------------------------------------------------------


def test_pydvdid() -> None:
    """Test that pydvdid-m is importable and functional."""
    banner("DVD Disc ID (pydvdid-m)")
    try:
        from pydvdid_m import compute
        print("  pydvdid-m: installed and importable")
        print("  Note: actual disc ID computation requires a mounted DVD")
    except ImportError:
        print("  pydvdid-m: NOT INSTALLED (pip install pydvdid-m)")


# ---------------------------------------------------------------------------
# BD disc ID test (can test the hash function on the rips if BDMV exists)
# ---------------------------------------------------------------------------


def test_bd_disc_id() -> None:
    """Test BD disc fingerprint logic."""
    banner("BD Disc ID (SHA-256 fingerprint)")
    print("  Note: actual computation requires a mounted Blu-ray disc")
    print("  The SHA-256 fingerprint hashes index.bdmv + MovieObject.bdmv")
    print("  These files are NOT AACS-encrypted and are readable on any BD mount")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("strophalos identification method validation")
    print("=" * 60)

    # Find test files
    spice_files: list[Path] = []
    mario_files: list[Path] = []

    if SPICE_WOLF_DIR.exists():
        # Just test first 2 files (4.3GB each — ed2k hashing takes time)
        all_sw = sorted(SPICE_WOLF_DIR.glob("*.mkv"))
        spice_files = all_sw[:2]
        print(f"Spice & Wolf: {len(all_sw)} files, testing {len(spice_files)}")
    else:
        print(f"Spice & Wolf: directory not found ({SPICE_WOLF_DIR})")

    if MARIO_DIR.exists():
        all_m = sorted(MARIO_DIR.glob("*.mkv"))
        mario_files = all_m[:2]
        print(f"Super Mario:  {len(all_m)} files, testing {len(mario_files)}")
    else:
        print(f"Super Mario:  directory not found ({MARIO_DIR})")

    if not spice_files and not mario_files:
        print("\nNo test files found. Exiting.")
        return

    # --- Hash computation ---
    if spice_files:
        test_hash_computation(spice_files, "Spice & Wolf (BD)")
    if mario_files:
        test_hash_computation(mario_files, "Super Mario Bros (DVD)")

    # --- SubDB lookups ---
    if mario_files:
        test_subdb_lookup(mario_files, "Super Mario Bros (DVD)")
    if spice_files:
        test_subdb_lookup(spice_files[:1], "Spice & Wolf (BD, 1 file)")

    # --- AniDB lookup (anime only — Spice & Wolf) ---
    if spice_files:
        test_anidb_lookup(spice_files, "Spice & Wolf (BD)", SPICE_WOLF_EXPECTED)

    # --- Import checks ---
    test_pydvdid()
    test_bd_disc_id()

    banner("Done")
    print("Review the output above for any ERRORs or unexpected results.")


if __name__ == "__main__":
    main()
