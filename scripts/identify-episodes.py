#!/usr/bin/env python3
"""Post-rip episode identification for TV disc rips.

Matches ripped MKV files to TMDb episodes using OpenSubtitles hash lookup,
duration filtering, subtitle text extraction, and scoring-based assignment.

Usage: identify-episodes.py --dir /output/bd/Show --label DISC_LABEL [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


def _opensubtitles_token_expired(config: dict[str, Any]) -> bool:
    """Check whether the stored JWT token has expired."""
    expires_str = config.get("expires", "")
    if not expires_str:
        return True
    try:
        expires = datetime.fromisoformat(expires_str)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires
    except Exception:
        return True


def _opensubtitles_refresh_token(config: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh an expired OpenSubtitles JWT token. Returns updated config or None."""
    import urllib.request

    api_key = config.get("api_key", "")
    if not api_key:
        return None

    # The v2 API does not have a dedicated refresh endpoint that works without
    # credentials. If the token is expired, we cannot refresh it without the
    # user's password. Log and return None so the caller falls back gracefully.
    print("  OpenSubtitles: JWT token expired. Re-run setup-opensubtitles.sh to re-authenticate.")
    _notify("OpenSubtitles token expired", "Run: docker exec -it strophalos setup-opensubtitles.sh", error=True)
    return None


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

    if _opensubtitles_token_expired(config):
        config = _opensubtitles_refresh_token(config)
        if config is None:
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


def fetch_all_episodes(label: str) -> tuple[list[Episode], list[Episode], str | None]:
    """Fetch all episodes for a series.

    Returns (regular_episodes, specials, group_name).
    Regular episodes sorted by (season, episode); specials sorted separately.
    """
    result = search_series(label)
    if not result:
        return [], [], None

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

    return regular, specials, group_name


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

    Returns (timestamp_seconds, text) pairs filtered to first 25% of duration.
    """
    tracks = list_subtitle_tracks(mkv_path)
    track = select_best_track(tracks)
    if not track:
        return []

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


def rename_from_hash_results(
    hash_results: dict[Path, tuple[int, int, str]],
    dry_run: bool,
) -> dict[str, dict[str, Any]]:
    """Rename files using OpenSubtitles hash identification results.

    Returns matched info dict keyed by new filename.
    """
    matched: dict[str, dict[str, Any]] = {}

    for path, (season, episode, title) in hash_results.items():
        code = f"S{season:02d}E{episode:02d}"
        title_safe = sanitize_filename(title)
        new_name = f"{code} - {title_safe}.mkv" if title_safe else f"{code}.mkv"
        new_path = path.parent / new_name

        if new_path.exists() and new_path != path:
            new_name = f"{code} - {title_safe} (2).mkv"
            new_path = path.parent / new_name

        action = "would rename" if dry_run else "rename"
        print(f"  {action}: {path.name} → {new_name}  (hash match)")

        if not dry_run:
            path.rename(new_path)

        matched[new_name] = {
            "original": path.name,
            "score": -1,  # Sentinel: hash match, not score-based
            "season": season,
            "episode": episode,
            "title": title,
            "method": "opensubtitles_hash",
        }

    return matched


def rename_files(
    files: list[RippedFile],
    assignments: list[tuple[int, int, float]],
    window_episodes: list[Episode],
    min_score: float,
    dry_run: bool,
) -> dict[str, dict[str, Any]]:
    """Rename files based on assignments. Returns matched info dict."""
    matched: dict[str, dict[str, Any]] = {}

    for file_idx, ep_idx, score in assignments:
        if score < min_score:
            continue

        f = files[file_idx]
        ep = window_episodes[ep_idx]
        title_safe = sanitize_filename(ep.title)
        new_name = f"{ep.code} - {title_safe}.mkv" if title_safe else f"{ep.code}.mkv"
        new_path = f.path.parent / new_name

        if new_path.exists() and new_path != f.path:
            new_name = f"{ep.code} - {title_safe} (2).mkv"
            new_path = f.path.parent / new_name

        action = "would rename" if dry_run else "rename"
        print(f"  {action}: {f.path.name} → {new_name}  (score={score:.2f})")

        if not dry_run:
            f.path.rename(new_path)

        matched[new_name] = {
            "original": f.path.name,
            "score": round(score, 2),
            "season": ep.season,
            "episode": ep.episode,
            "title": ep.title,
        }

    return matched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Episode identification for TV disc rips")
    parser.add_argument("--dir", required=True, help="Directory containing ripped MKV files")
    parser.add_argument("--label", required=True, help="Disc label for TMDb search")
    parser.add_argument("--dry-run", action="store_true", help="Show proposed renames without executing")
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
    mkv_files = sorted(out_dir.glob("*.mkv"))
    if not mkv_files:
        print("No MKV files found")
        return

    print(f"Found {len(mkv_files)} MKV file(s)")

    # --- Layer 0: OpenSubtitles hash-based identification ---
    # This is the most reliable method when a hash match exists in the DB.
    # If it identifies ALL files, we can skip subtitle extraction entirely.
    hash_results = opensubtitles_identify(mkv_files)
    if hash_results and len(hash_results) == len(mkv_files):
        print("  OpenSubtitles identified all files — skipping TMDb/subtitle pipeline")
        matched = rename_from_hash_results(hash_results, args.dry_run)
        unmatched = [p.name for p in mkv_files if p not in hash_results]
        if not args.dry_run and matched:
            manifest = {
                "series": _clean_disc_label(args.label),
                "episode_group": "OpenSubtitles hash",
                "matched": matched,
                "unmatched": unmatched,
                "timestamp": datetime.now().isoformat(),
            }
            manifest_path = out_dir / ".episode-manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2))
            print(f"  Manifest written: {manifest_path}")
        ep_range = f"{list(matched.values())[0]['season']}x{list(matched.values())[0]['episode']:02d}–{list(matched.values())[-1]['season']}x{list(matched.values())[-1]['episode']:02d}" if matched else ""
        summary = f"Matched {len(matched)}/{len(mkv_files)} file(s) via OpenSubtitles hash"
        print(summary)
        series_name = _clean_disc_label(args.label)
        _notify(
            f"{series_name} — {len(matched)} episodes identified",
            f"{ep_range}\nMethod: OpenSubtitles hash\n{len(mkv_files)} title(s) ripped",
        )
        return

    # If hash lookup got partial results, store them — we'll use them to
    # pre-assign those files and only run the TMDb pipeline for the rest.
    hash_identified: dict[Path, tuple[int, int, str]] = hash_results or {}

    # Fetch episodes from TMDb
    episodes, specials, group_name = fetch_all_episodes(args.label)
    if not episodes:
        print("No episodes found, skipping identification")
        return

    n_seasons = len({ep.season for ep in episodes})
    print(f"  {len(episodes)} episodes across {n_seasons} season(s)")
    if specials:
        print(f"  {len(specials)} special(s) available for leftover matching")

    # Build RippedFile objects with durations
    files: list[RippedFile] = []
    for mkv in mkv_files:
        dur = get_mkv_duration(mkv)
        files.append(RippedFile(path=mkv, duration_seconds=dur))
        print(f"  {mkv.name}: {dur:.0f}s")

    # Handle partial OpenSubtitles results: rename hash-identified files now
    # and exclude them (and their matching episodes) from the scoring pipeline.
    hash_matched: dict[str, dict[str, Any]] = {}
    if hash_identified:
        hash_matched = rename_from_hash_results(hash_identified, args.dry_run)
        hash_ep_keys = {(s, e) for s, e, _ in hash_identified.values()}
        # Remove hash-identified files from the scoring pipeline
        files = [f for f in files if f.path not in hash_identified]
        # Remove their episodes from candidate lists
        episodes = [ep for ep in episodes if (ep.season, ep.episode) not in hash_ep_keys]
        if not files:
            print("  All remaining files identified via OpenSubtitles hash")
            if not args.dry_run and hash_matched:
                manifest = {
                    "series": _clean_disc_label(args.label),
                    "episode_group": group_name or "OpenSubtitles hash + TMDb",
                    "matched": hash_matched,
                    "unmatched": [],
                    "timestamp": datetime.now().isoformat(),
                }
                manifest_path = out_dir / ".episode-manifest.json"
                manifest_path.write_text(json.dumps(manifest, indent=2))
            print(f"Matched {len(hash_matched)}/{len(mkv_files)} file(s)")
            return
        if not episodes:
            print("  No remaining episodes to match against after hash identification")
            return
        print(f"  {len(hash_identified)} file(s) resolved via hash, {len(files)} remaining for scoring")

    # Duration filter: check which episodes could match each file
    need_subs = False
    for f in files:
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

        with ThreadPoolExecutor(max_workers=min(4, len(files))) as pool:
            futures = {pool.submit(_process_file, f): f for f in files}
            for future in as_completed(futures):
                f, n_cues = future.result()
                print(f"  {f.path.name}: {n_cues} subtitle cue(s)")

        # Deduplicate: remove subtitle cues that appear in multiple files
        # (OP/ED lyrics, credits overlays are identical across episodes)
        if len(files) > 1:
            cue_counts: Counter[str] = Counter()
            for f in files:
                # Count each unique text once per file
                seen: set[str] = set()
                for _, text in f.subtitle_texts:
                    normalized = text.strip().lower()
                    if normalized not in seen:
                        cue_counts[normalized] += 1
                        seen.add(normalized)

            # Remove cues that appear in more than half the files
            threshold = len(files) // 2
            common_cues = {text for text, count in cue_counts.items() if count > threshold}
            if common_cues:
                for f in files:
                    before = len(f.subtitle_texts)
                    f.subtitle_texts = [
                        (t, text) for t, text in f.subtitle_texts
                        if text.strip().lower() not in common_cues
                    ]
                    after = len(f.subtitle_texts)
                    if before != after:
                        print(f"  {f.path.name}: {before - after} common cues removed, {after} unique")

        if args.verbose:
            for f in files:
                if f.subtitle_texts:
                    print(f"  {f.path.name} unique cues:")
                    for t, text in f.subtitle_texts[:10]:
                        print(f"    [{t:.1f}s] {text[:100]}")
    else:
        print("  Duration matching is sufficient, skipping subtitle extraction")

    # Check for already-identified episodes in the output directory
    # (from previous disc rips). These tell us where to continue from.
    existing_eps: set[tuple[int, int]] = set()
    for existing in out_dir.glob("S[0-9][0-9]E[0-9][0-9]*.mkv"):
        m = re.match(r"S(\d+)E(\d+)", existing.name)
        if m:
            existing_eps.add((int(m.group(1)), int(m.group(2))))

    if existing_eps:
        print(f"  Found {len(existing_eps)} already-identified episode(s) in directory")
        # Remove already-identified episodes from candidates
        episodes = [ep for ep in episodes if (ep.season, ep.episode) not in existing_eps]
        if not episodes:
            print("  All episodes already identified, nothing to do")
            return

    # Compute word weights for scoring
    word_weights = compute_word_weights(episodes)

    # Find best contiguous window
    print("  Finding best episode window...")
    window_start, window_score = find_best_window(files, episodes, word_weights)
    window = episodes[window_start : window_start + len(files)]

    if window:
        print(f"  Best window: {window[0].code}–{window[-1].code} (score={window_score:.2f})")
    else:
        print("  Could not determine episode window")
        return

    # If subtitle scoring is weak (all scores near-equal), fall back to
    # forward ordering from the earliest available episode.
    # This handles the common case: disc N has the next batch of episodes.
    scores_by_window: list[float] = []
    n = len(files)
    for start in range(max(1, len(episodes) - n + 1)):
        w = episodes[start : start + n]
        if len(w) == n:
            m = _build_score_matrix(files, w, word_weights)
            a = hungarian_assignment(m)
            scores_by_window.append(sum(m[i][j] for i, j, _ in a if i < n and j < n))

    use_forward_order = False
    if scores_by_window:
        best = max(scores_by_window)
        second = sorted(scores_by_window, reverse=True)[1] if len(scores_by_window) > 1 else 0
        margin = (best - second) / best if best > 0 else 0
        if margin < 0.15 and assume_order:
            # Scores too close — subtitle matching isn't discriminating.
            # Prefer the earliest window (forward from where we left off).
            window = episodes[:n]
            print(f"  Scores indistinct (margin={margin:.0%}), using forward order: {window[0].code}–{window[-1].code}")
            use_forward_order = True
        elif margin < 0.15:
            print(f"  Scores indistinct (margin={margin:.0%}), ASSUME_DISC_ORDER=false — using best-scoring window")

    if use_forward_order:
        # Direct forward assignment: t00→first ep, t01→second, etc.
        # No min_score gating — we're committing to this ordering.
        assignments = [(i, i, 1.0) for i in range(len(files)) if i < len(window)]
        min_score = 0.0
    else:
        assignments = assign_episodes(files, window, word_weights)
        min_score = args.min_score

    # Rename
    matched = rename_files(files, assignments, window, min_score, args.dry_run)

    # Determine unmatched files
    matched_indices = {a[0] for a in assignments if a[2] >= min_score}
    unmatched_files = [files[i] for i in range(len(files)) if i not in matched_indices]

    # Try matching leftovers against specials (short extras, OVAs, etc.)
    if unmatched_files and specials:
        print(f"  Trying {len(unmatched_files)} unmatched file(s) against specials...")
        sp_weights = compute_word_weights(specials)
        sp_matrix = _build_score_matrix(unmatched_files, specials, sp_weights)
        sp_assignments = hungarian_assignment(sp_matrix)
        sp_matched = rename_files(
            unmatched_files, sp_assignments, specials, args.min_score, args.dry_run,
        )
        matched.update(sp_matched)
        # Recalculate unmatched
        sp_matched_indices = {a[0] for a in sp_assignments if a[2] >= args.min_score}
        unmatched_files = [f for i, f in enumerate(unmatched_files) if i not in sp_matched_indices]

    unmatched = [f.path.name for f in unmatched_files]
    if unmatched:
        print(f"  Unmatched files: {unmatched}")

    # Merge in any partial hash results from earlier
    matched.update(hash_matched)
    total_files = len(files) + len(hash_matched)

    # Write manifest
    if not args.dry_run and matched:
        manifest = {
            "series": _clean_disc_label(args.label),
            "episode_group": group_name,
            "matched": matched,
            "unmatched": unmatched,
            "timestamp": datetime.now().isoformat(),
        }
        manifest_path = out_dir / ".episode-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  Manifest written: {manifest_path}")

    summary = f"Matched {len(matched)}/{total_files} file(s)"
    if unmatched:
        summary += f", {len(unmatched)} unmatched"
    print(summary)

    # Build notification with method and episode range
    series_name = _clean_disc_label(args.label)
    methods: set[str] = set()
    if hash_identified:
        methods.add("OpenSubtitles hash")
    if use_forward_order:
        methods.add("forward order")
    elif any(a[2] > 0 for a in assignments):
        methods.add("subtitle matching")
    method_str = " + ".join(sorted(methods)) or "duration"

    matched_eps = sorted(matched.values(), key=lambda m: (m.get("season", 0), m.get("episode", 0)))
    if matched_eps:
        first, last = matched_eps[0], matched_eps[-1]
        ep_range = f"S{first['season']:02d}E{first['episode']:02d}–S{last['season']:02d}E{last['episode']:02d}"
    else:
        ep_range = ""

    body = f"{ep_range}\nMethod: {method_str}\n{total_files} title(s) ripped"
    if unmatched:
        body += f"\n{len(unmatched)} unmatched"
        _notify(f"{series_name} — partially identified", body, error=True)
    elif matched:
        _notify(f"{series_name} — {len(matched)} episodes identified", body)


if __name__ == "__main__":
    main()
