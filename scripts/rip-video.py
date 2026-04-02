#!/usr/bin/env python3
"""Smart video disc ripper — analyzes title structure to skip play-all
playlists and detect TV vs movie discs.

Usage: rip-video.py [--drive N] [--output /output] [--dry-run]
"""

import argparse
import os
import re
import subprocess
import sys
from collections import Counter

MAKEMKV = "makemkvcon"


def scan_disc(drive_id):
    """Scan disc and return parsed title info + disc metadata."""
    cmd = [MAKEMKV, "-r", "info", f"disc:{drive_id}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr

    titles = {}
    disc_label = None
    disc_info = {}

    for line in output.splitlines():
        # CINFO:attr_id,code,"value"
        m = re.match(r'CINFO:(\d+),\d+,"(.+)"', line)
        if m:
            attr_id, val = int(m.group(1)), m.group(2)
            disc_info[attr_id] = val
            if attr_id == 2:
                disc_label = val

        # TINFO:title_id,attribute_id,code,"value"
        m = re.match(r'TINFO:(\d+),(\d+),\d+,"(.+)"', line)
        if m:
            tid, attr, val = int(m.group(1)), int(m.group(2)), m.group(3)
            if tid not in titles:
                titles[tid] = {}
            titles[tid][attr] = val

    return disc_label, titles, disc_info


def parse_duration(duration_str):
    """Parse H:MM:SS or M:SS to seconds."""
    parts = duration_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


def get_title_durations(titles):
    """Extract title ID -> duration in seconds. Attribute 9 = duration."""
    durations = {}
    for tid, attrs in titles.items():
        if 9 in attrs:
            durations[tid] = parse_duration(attrs[9])
    return durations


def get_title_chapters(titles):
    """Extract title ID -> chapter count. Attribute 8 = chapter count."""
    chapters = {}
    for tid, attrs in titles.items():
        if 8 in attrs:
            chapters[tid] = int(attrs[8])
    return chapters


def _parse_size(s):
    """Parse a size string — raw bytes or formatted like '27.3 GB'."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    m = re.match(r'([\d.]+)\s*(TB|GB|MB|KB)', s, re.IGNORECASE)
    if m:
        num = float(m.group(1))
        unit = m.group(2).upper()
        return int(num * {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}[unit])
    return 0


def get_title_sizes(titles):
    """Extract title ID -> output file size in bytes. Attribute 10 = size."""
    sizes = {}
    for tid, attrs in titles.items():
        if 10 in attrs:
            sizes[tid] = _parse_size(attrs[10])
    return sizes


def detect_media_type(disc_info):
    """Detect physical media type: 'dvd', 'bd', or 'uhd'.

    Uses CINFO:1 (disc type string) from the makemkvcon scan.
    MakeMKV reports UHD discs as e.g. "Blu-ray disc (AACS v2)" or similar.
    """
    # CINFO attribute 1 = disc type description
    disc_type_str = disc_info.get(1, "")
    print(f"  Media: disc_type='{disc_type_str}'")

    lower = disc_type_str.lower()
    if "dvd" in lower:
        return "dvd"
    if "uhd" in lower or "aacs v2" in lower or "aacs2" in lower:
        return "uhd"
    if "blu-ray" in lower or "bd" in lower:
        return "bd"
    return "bd"


def _score_movie(sorted_titles, longest_tid, longest_dur, rest, meaningful):
    """Score how well the disc fits a movie pattern. Returns (score, titles, reason)."""
    score = 0.0
    reasons = []

    # Duration dominance: main feature much longer than anything else
    if rest:
        second_dur = rest[0][1]
        if second_dur > 0:
            ratio = longest_dur / second_dur
            if ratio >= 4:
                score += 0.3
                reasons.append(f"dominant ({ratio:.1f}x)")
            elif ratio >= 2:
                score += 0.15
                reasons.append(f"longer ({ratio:.1f}x)")

    # Feature length (>= 1 hour)
    if longest_dur >= 3600:
        score += 0.2
        reasons.append("feature-length")

    # Extras pattern: everything else is much shorter than main
    if rest:
        extras = [(tid, dur) for tid, dur in rest if dur < longest_dur * 0.5]
        if len(extras) == len(rest):
            score += 0.2
            reasons.append("short extras")

    # Few non-trivial extras — movies typically have 0-3, not 9
    non_trivial = [(tid, dur) for tid, dur in rest if dur >= 600]
    if len(non_trivial) <= 3:
        score += 0.2
        reasons.append(f"{len(non_trivial)} non-trivial extra(s)")
    elif len(non_trivial) <= 6:
        score += 0.1

    return score, [longest_tid], "; ".join(reasons)


def _score_tv(sorted_titles, longest_tid, longest_dur, rest, meaningful):
    """Score how well the disc fits a TV pattern. Returns (score, titles, reason)."""
    score = 0.0
    reasons = []

    rest_meaningful = [(tid, dur) for tid, dur in meaningful if tid != longest_tid]
    episode_candidates = [(tid, dur) for tid, dur in rest_meaningful if dur >= 600]

    if len(episode_candidates) < 2:
        return 0.0, [], "too few episode candidates"

    ep_durs = [dur for _, dur in episode_candidates]
    median_dur = sorted(ep_durs)[len(ep_durs) // 2]

    in_cluster = [(tid, dur) for tid, dur in episode_candidates
                  if median_dur * 0.5 <= dur <= median_dur * 2.0]

    if len(in_cluster) < 2:
        return 0.0, [], "no duration cluster"

    # Episode uniformity — low coefficient of variation means consistent lengths
    cluster_durs = [dur for _, dur in in_cluster]
    mean_dur = sum(cluster_durs) / len(cluster_durs)
    variance = sum((d - mean_dur) ** 2 for d in cluster_durs) / len(cluster_durs)
    cv = (variance ** 0.5) / mean_dur if mean_dur > 0 else 1.0

    if cv < 0.05:
        score += 0.35
        reasons.append(f"very uniform (CV {cv:.3f})")
    elif cv < 0.15:
        score += 0.25
        reasons.append(f"uniform (CV {cv:.2f})")
    elif cv < 0.30:
        score += 0.15
        reasons.append(f"somewhat uniform (CV {cv:.2f})")

    # Episode count — more similar titles = stronger TV signal
    if len(in_cluster) >= 6:
        score += 0.3
        reasons.append(f"{len(in_cluster)} episodes")
    elif len(in_cluster) >= 3:
        score += 0.2
        reasons.append(f"{len(in_cluster)} episodes")
    else:
        score += 0.05
        reasons.append(f"{len(in_cluster)} episodes")

    # Play-all detection — longest ≈ sum of cluster
    cluster_sum = sum(dur for _, dur in in_cluster)
    if cluster_sum > 0:
        play_all_diff = abs(longest_dur - cluster_sum) / cluster_sum
        if play_all_diff < 0.10:
            score += 0.3
            reasons.append(f"play-all (title {longest_tid}, {play_all_diff:.1%} off)")

    # Typical episode duration (20–65 min)
    if 1200 <= median_dur <= 3900:
        score += 0.1
        reasons.append("typical episode length")

    # Select episode cluster; include longest only if it's episode-length itself
    titles_to_rip = [tid for tid, _ in in_cluster]
    if median_dur * 0.5 <= longest_dur <= median_dur * 2.0:
        if longest_tid not in titles_to_rip:
            titles_to_rip.append(longest_tid)

    return score, sorted(titles_to_rip), "; ".join(reasons)


def _score_title_search(disc_label):
    """Query TMDb for the disc title. Returns (movie_boost, tv_boost).

    Requires TMDB_API_KEY env var; returns (0, 0) if unset or on failure.
    """
    import json
    import urllib.parse
    import urllib.request

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key or not disc_label:
        return 0.0, 0.0

    query = disc_label.replace("_", " ").strip()
    if not query:
        return 0.0, 0.0

    url = (f"https://api.themoviedb.org/3/search/multi"
           f"?api_key={api_key}&query={urllib.parse.quote(query)}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        results = data.get("results", [])
        if not results:
            print(f"  TMDb: no results for '{query}'")
            return 0.0, 0.0

        top = results[:3]
        types = [r.get("media_type", "") for r in top]
        names = [r.get("title") or r.get("name", "?") for r in top]
        print(f"  TMDb: {list(zip(names, types))}")

        tv_count = types.count("tv")
        movie_count = types.count("movie")

        if tv_count > movie_count:
            return 0.0, 0.3
        elif movie_count > tv_count:
            return 0.3, 0.0
    except Exception as e:
        print(f"  TMDb: search failed ({e})")

    return 0.0, 0.0


def classify_disc(durations, chapters, disc_label=None):
    """Classify disc as 'tv', 'movie', or 'unknown' and return titles to rip.

    Scores both patterns independently, uses TMDb as a signal when available,
    and picks the higher-scoring classification.

    Returns (disc_type, titles_to_rip, reason)
    """
    if not durations:
        return "unknown", [], "no titles found"

    sorted_titles = sorted(durations.items(), key=lambda x: x[1], reverse=True)
    longest_tid, longest_dur = sorted_titles[0]
    rest = sorted_titles[1:]

    meaningful = [(tid, dur) for tid, dur in sorted_titles if dur >= 120]

    if len(meaningful) == 0:
        return "unknown", [t[0] for t in sorted_titles], "no meaningful titles"

    if len(meaningful) == 1:
        return "movie", [meaningful[0][0]], "single main title"

    # Score both patterns
    m_score, m_titles, m_reason = _score_movie(
        sorted_titles, longest_tid, longest_dur, rest, meaningful)
    t_score, t_titles, t_reason = _score_tv(
        sorted_titles, longest_tid, longest_dur, rest, meaningful)

    # Optional title search boost
    m_boost, t_boost = _score_title_search(disc_label)
    m_score += m_boost
    t_score += t_boost

    search_note = ""
    if m_boost or t_boost:
        search_note = f", search: +{m_boost:.1f}m/+{t_boost:.1f}t"

    print(f"\n  Scores: movie={m_score:.2f} [{m_reason}]")
    print(f"          tv={t_score:.2f} [{t_reason}]")

    if t_score > m_score:
        return ("tv", t_titles,
                f"tv={t_score:.2f} > movie={m_score:.2f}{search_note}")
    elif m_score > t_score:
        return ("movie", m_titles,
                f"movie={m_score:.2f} > tv={t_score:.2f}{search_note}")
    else:
        return ("unknown",
                [tid for tid, _ in meaningful],
                f"tied {m_score:.2f}{search_note}, ripping all {len(meaningful)}")


def rip_titles(drive_id, title_ids, output_dir, min_length=None):
    """Rip specific titles from disc."""
    os.makedirs(output_dir, exist_ok=True)

    opts = "-r"
    if min_length:
        opts += f" --minlength={min_length}"

    for tid in title_ids:
        print(f"  Ripping title {tid}...")
        cmd = [MAKEMKV] + opts.split() + ["mkv", f"disc:{drive_id}", str(tid), output_dir]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  WARNING: title {tid} failed (rc={result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[-500:]}")
            if result.stdout:
                # Print last few lines of stdout for error context
                lines = result.stdout.strip().split('\n')
                for line in lines[-5:]:
                    print(f"  > {line}")


def main():
    parser = argparse.ArgumentParser(description="Smart video disc ripper")
    parser.add_argument("--drive", type=int, default=0, help="MakeMKV drive ID")
    parser.add_argument("--output", default="/output", help="Output base directory")
    parser.add_argument("--dry-run", action="store_true", help="Scan and classify only")
    args = parser.parse_args()

    print(f"Scanning disc in drive {args.drive}...")
    disc_label, titles, disc_info = scan_disc(args.drive)
    print(f"Disc label: {disc_label}")
    print(f"Found {len(titles)} title(s)")

    durations = get_title_durations(titles)
    chapters = get_title_chapters(titles)
    sizes = get_title_sizes(titles)

    print("\nTitle list:")
    for tid in sorted(durations.keys()):
        dur = durations[tid]
        ch = chapters.get(tid, "?")
        sz = sizes.get(tid, 0)
        mins, secs = divmod(dur, 60)
        hours, mins = divmod(mins, 60)
        sz_gb = sz / (1024 ** 3) if sz else 0
        size_str = f", {sz_gb:.1f} GB" if sz else ""
        print(f"  Title {tid:2d}: {hours}:{mins:02d}:{secs:02d}  ({ch} chapters{size_str})")

    disc_type, to_rip, reason = classify_disc(durations, chapters, disc_label)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    media_type = detect_media_type(disc_info)
    print(f"Media type: {media_type}")

    if args.dry_run:
        print("\n[dry-run] Would rip the above titles.")
        return

    out_dir = os.path.join(args.output, media_type, disc_label or "unknown_disc")

    print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
    rip_titles(args.drive, to_rip, out_dir)
    # Print output dir on a known-format line so auto-rip.sh can capture it
    print(f"STROPHALOS_OUTPUT_DIR={out_dir}")
    print(f"STROPHALOS_DISC_TYPE={disc_type}")
    print("Done.")


if __name__ == "__main__":
    main()
