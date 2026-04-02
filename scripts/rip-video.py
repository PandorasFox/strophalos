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
    """Scan disc and return parsed title info."""
    cmd = [MAKEMKV, "-r", "info", f"disc:{drive_id}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr

    titles = {}
    disc_label = None

    for line in output.splitlines():
        # CINFO:2,0,"DISC_LABEL"
        m = re.match(r'CINFO:2,\d+,"(.+)"', line)
        if m:
            disc_label = m.group(1)

        # TINFO:title_id,attribute_id,code,"value"
        m = re.match(r'TINFO:(\d+),(\d+),\d+,"(.+)"', line)
        if m:
            tid, attr, val = int(m.group(1)), int(m.group(2)), m.group(3)
            if tid not in titles:
                titles[tid] = {}
            titles[tid][attr] = val

    return disc_label, titles


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


def classify_disc(durations, chapters):
    """Classify disc as 'tv', 'movie', or 'unknown' and return titles to rip.

    Returns (disc_type, titles_to_rip, reason)
    """
    if not durations:
        return "unknown", [], "no titles found"

    sorted_titles = sorted(durations.items(), key=lambda x: x[1], reverse=True)
    longest_tid, longest_dur = sorted_titles[0]
    rest = sorted_titles[1:]

    # Filter out very short titles (< 2 min) — menus, intros, etc.
    meaningful = [(tid, dur) for tid, dur in sorted_titles if dur >= 120]
    short = [(tid, dur) for tid, dur in sorted_titles if dur < 120]

    if len(meaningful) == 0:
        return "unknown", [t[0] for t in sorted_titles], "no meaningful titles"

    if len(meaningful) == 1:
        # Single meaningful title — movie or single feature
        return "movie", [meaningful[0][0]], "single main title"

    # Movie pattern: one long title (>= 1 hour), everything else is short
    # Check this FIRST — a movie with extras is more common than misclassifying
    if longest_dur >= 3600:
        extras = [(tid, dur) for tid, dur in rest if dur < longest_dur * 0.5]
        if len(extras) == len(rest):
            return ("movie",
                    [longest_tid],
                    f"main feature ({longest_dur}s), {len(extras)} extras skipped")

    # TV pattern: cluster of similar-length titles (>= 10 min each)
    # Exclude the longest, see if the rest cluster together
    rest_meaningful = [(tid, dur) for tid, dur in meaningful if tid != longest_tid]
    # Only consider titles >= 10 min as potential episodes
    episode_candidates = [(tid, dur) for tid, dur in rest_meaningful if dur >= 600]

    if len(episode_candidates) >= 2:
        ep_durs = [dur for _, dur in episode_candidates]
        median_dur = sorted(ep_durs)[len(ep_durs) // 2]

        # Allow episodes to vary: within 50-200% of median covers double-length
        # pilots/finales and standard variation
        in_cluster = [(tid, dur) for tid, dur in episode_candidates
                      if median_dur * 0.5 <= dur <= median_dur * 2.0]

        cluster_sum = sum(dur for _, dur in in_cluster)

        # Is the longest title approximately the sum of the cluster?
        # (within 10% — play-all playlists may have slight differences)
        if len(in_cluster) >= 3 and abs(longest_dur - cluster_sum) / cluster_sum < 0.10:
            return ("tv",
                    [tid for tid, _ in in_cluster],
                    f"detected play-all (title {longest_tid}: {longest_dur}s ≈ "
                    f"sum of {len(in_cluster)} episodes: {cluster_sum}s)")

        # No obvious play-all, but still multiple similar titles
        if len(in_cluster) >= 3:
            # Check if longest is just another episode (within cluster range)
            if median_dur * 0.5 <= longest_dur <= median_dur * 2.0:
                return ("tv",
                        [tid for tid, _ in meaningful if dur >= 600
                         or (tid == longest_tid)],
                        f"{len(in_cluster)} episodes, no play-all detected")
            else:
                # Longest is outside episode range but not a clear play-all sum
                return ("tv",
                        [tid for tid, _ in in_cluster],
                        f"{len(in_cluster)} episodes, excluded outlier title "
                        f"{longest_tid} ({longest_dur}s)")

    # Fallback: can't determine, rip everything meaningful
    return ("unknown",
            [tid for tid, _ in meaningful],
            f"could not classify, ripping {len(meaningful)} titles")


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
    disc_label, titles = scan_disc(args.drive)
    print(f"Disc label: {disc_label}")
    print(f"Found {len(titles)} title(s)")

    durations = get_title_durations(titles)
    chapters = get_title_chapters(titles)

    print("\nTitle list:")
    for tid in sorted(durations.keys()):
        dur = durations[tid]
        ch = chapters.get(tid, "?")
        mins, secs = divmod(dur, 60)
        hours, mins = divmod(mins, 60)
        print(f"  Title {tid:2d}: {hours}:{mins:02d}:{secs:02d}  ({ch} chapters)")

    disc_type, to_rip, reason = classify_disc(durations, chapters)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    if args.dry_run:
        print("\n[dry-run] Would rip the above titles.")
        return

    out_dir = os.path.join(args.output, disc_label or "unknown_disc")
    if os.path.exists(out_dir):
        out_dir += f"-{os.getpid()}"

    print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
    rip_titles(args.drive, to_rip, out_dir)
    # Print output dir on a known-format line so auto-rip.sh can capture it
    print(f"STROPHALOS_OUTPUT_DIR={out_dir}")
    print("Done.")


if __name__ == "__main__":
    main()
