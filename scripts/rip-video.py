#!/usr/bin/env python3
"""Smart video disc ripper — analyzes title structure to skip play-all
playlists and detect TV vs movie discs.

Usage: rip-video.py [--drive N] [--output /output] [--dry-run]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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


def _score_musicbrainz(disc_label, durations):
    """Check MusicBrainz for a matching release. Returns (music_score, release_info).

    Searches by disc label against the local MusicBrainz replica. If a release
    matches and track durations align with title durations, returns a high score.

    Returns (score, {artist, title, id}) or (0.0, None).
    """
    import json
    import urllib.parse
    import urllib.request

    mb_server = os.environ.get("MB_SERVER", "mb-web:5000")
    query = disc_label.replace("_", " ").strip()
    if not query:
        return 0.0, None

    # Search releases by name
    url = (f"http://{mb_server}/ws/2/release/"
           f"?query=release:{urllib.parse.quote(query)}&fmt=json&limit=5")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "strophalos/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  MusicBrainz: search failed ({e})")
        return 0.0, None

    releases = data.get("releases", [])
    if not releases:
        return 0.0, None

    # Get title durations as a sorted list for comparison
    title_durs = sorted(durations.values())
    n_titles = len(title_durs)

    best_score = 0.0
    best_release = None

    for rel in releases:
        rel_id = rel.get("id", "")
        rel_title = rel.get("title", "")
        artist = ""
        if rel.get("artist-credit"):
            artist = rel["artist-credit"][0].get("name", "")

        # Get track durations from the release's media
        track_durs = []
        for medium in rel.get("media", []):
            for track in medium.get("tracks", []):
                length_ms = track.get("length")
                if length_ms:
                    track_durs.append(length_ms / 1000.0)  # ms → seconds

        if not track_durs:
            # Need to fetch full release for track info
            try:
                detail_url = (f"http://{mb_server}/ws/2/release/{rel_id}"
                              f"?inc=recordings+media&fmt=json")
                req = urllib.request.Request(detail_url, headers={"User-Agent": "strophalos/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    detail = json.loads(resp.read())
                for medium in detail.get("media", []):
                    for track in medium.get("tracks", []):
                        rec = track.get("recording", {})
                        length_ms = rec.get("length") or track.get("length")
                        if length_ms:
                            track_durs.append(int(length_ms) / 1000.0)
            except Exception:
                continue

        if not track_durs:
            continue

        # Compare track count
        if len(track_durs) != n_titles:
            continue

        # Compare durations — sort both and check alignment
        sorted_tracks = sorted(track_durs)
        total_diff = 0.0
        for td, rd in zip(title_durs, sorted_tracks):
            total_diff += abs(td - rd)

        avg_diff = total_diff / n_titles
        # Audio tracks should match within ~2 seconds on average
        if avg_diff < 5.0:
            score = max(0, 1.0 - avg_diff / 5.0)
            if score > best_score:
                best_score = score
                best_release = {
                    "artist": artist,
                    "title": rel_title,
                    "id": rel_id,
                    "track_count": len(track_durs),
                    "avg_diff": avg_diff,
                }

    if best_release:
        print(f"  MusicBrainz: matched '{query}' → {best_release['artist']} - {best_release['title']}"
              f" ({best_release['track_count']} tracks, avg diff {best_release['avg_diff']:.1f}s)")

    return best_score, best_release


def classify_disc(durations, chapters, disc_label=None):
    """Classify disc as 'tv', 'movie', 'music', or 'unknown' and return titles to rip.

    Scores TV, movie, and music patterns independently, uses TMDb and
    MusicBrainz as signals, and picks the highest-scoring classification.

    Returns (disc_type, titles_to_rip, reason)
    """
    if not durations:
        return "unknown", [], "no titles found"

    sorted_titles = sorted(durations.items(), key=lambda x: x[1], reverse=True)
    longest_tid, longest_dur = sorted_titles[0]
    rest = sorted_titles[1:]

    meaningful = [(tid, dur) for tid, dur in sorted_titles if dur >= 120]

    # Check MusicBrainz first — audio BDs have many short tracks that
    # would be filtered out by the >=120s threshold, so use ALL titles
    mb_score, mb_release = _score_musicbrainz(disc_label, durations)
    if mb_score > 0.7:
        # Strong MB match — this is an audio BD, rip all titles
        return ("music",
                list(durations.keys()),
                f"musicbrainz match: {mb_release['artist']} - {mb_release['title']} "
                f"(score={mb_score:.2f}, {mb_release['track_count']} tracks)")

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

    # Weak MB match can still boost against movie/tv
    if mb_score > 0.3:
        print(f"  MusicBrainz: weak match (score={mb_score:.2f}), not overriding")

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
    import time as _time

    os.makedirs(output_dir, exist_ok=True)

    opts = "-r"
    if min_length:
        opts += f" --minlength={min_length}"

    PROGRESS_INTERVAL = 180  # seconds between progress log lines

    for i, tid in enumerate(title_ids):
        print(f"  Ripping title {tid} ({i + 1}/{len(title_ids)})...", flush=True)
        cmd = [MAKEMKV] + opts.split() + ["mkv", f"disc:{drive_id}", str(tid), output_dir]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        last_progress_time = _time.monotonic()
        last_pct = -1
        output_tail: list[str] = []

        # In robot mode (-r), makemkvcon sends PRGV progress to stdout
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            output_tail.append(line)
            if len(output_tail) > 20:
                output_tail.pop(0)

            # PRGV:current,total,max — progress values
            if line.startswith("PRGV:"):
                parts = line[5:].split(",")
                if len(parts) >= 3:
                    try:
                        current, total, pmax = int(parts[0]), int(parts[1]), int(parts[2])
                        pct = int(current * 100 / pmax) if pmax > 0 else 0
                        now = _time.monotonic()
                        if pct != last_pct and (now - last_progress_time) >= PROGRESS_INTERVAL:
                            print(f"  title {tid}: {pct}% ({i + 1}/{len(title_ids)})", flush=True)
                            last_progress_time = now
                            last_pct = pct
                    except (ValueError, ZeroDivisionError):
                        pass

        proc.wait()
        if proc.returncode != 0:
            print(f"  WARNING: title {tid} failed (rc={proc.returncode})")
            for sl in output_tail[-5:]:
                print(f"  > {sl}")
            if proc.stderr:
                for sl in proc.stderr.read().strip().split('\n')[-5:]:
                    if sl:
                        print(f"  > {sl}")


# ---------------------------------------------------------------------------
# Disc ID computation
# ---------------------------------------------------------------------------


def _mount_disc_readonly(device: str) -> str | None:
    """Mount a disc read-only to a temp directory. Returns mount point or None.

    Tries UDF first (Blu-ray), then ISO 9660 (DVD), then auto-detect.
    Caller MUST call _unmount_disc() when done.
    """
    mount_point = tempfile.mkdtemp(prefix="strophalos-disc-")

    for fs_type in ("udf", "iso9660", "auto"):
        opts = ["mount", "-o", "ro"]
        if fs_type != "auto":
            opts += ["-t", fs_type]
        opts += [device, mount_point]

        result = subprocess.run(opts, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return mount_point

    os.rmdir(mount_point)
    return None


def _unmount_disc(mount_point: str) -> None:
    """Unmount and remove temp mount point."""
    subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
    try:
        os.rmdir(mount_point)
    except OSError:
        pass


def compute_dvd_disc_id(device: str) -> str | None:
    """Compute the standard DVD disc ID (CRC64 from VIDEO_TS IFO files).

    Uses the same algorithm as Windows IDvdInfo2::GetDiscID — compatible with
    pydvdid/dvdid databases. Requires mounting the disc to read IFO files
    (which are never CSS-encrypted).
    """
    try:
        from pydvdid_m import compute
    except ImportError:
        print("  DVD disc ID: pydvdid-m not installed, skipping")
        return None

    mount_point = _mount_disc_readonly(device)
    if not mount_point:
        print("  DVD disc ID: could not mount disc")
        return None

    try:
        disc_id = compute(mount_point)
        result = str(disc_id).lower()
        print(f"  DVD disc ID: {result}")
        return result
    except Exception as e:
        print(f"  DVD disc ID: computation failed: {e}")
        return None
    finally:
        _unmount_disc(mount_point)


def compute_bd_disc_id(device: str) -> str | None:
    """Compute a Blu-ray disc fingerprint.

    SHA-256 of the disc's index.bdmv + MovieObject.bdmv metadata files,
    which are not AACS-encrypted and uniquely identify the disc's content
    structure. Returns the hex digest or None.
    """
    mount_point = _mount_disc_readonly(device)
    if not mount_point:
        print("  BD disc ID: could not mount disc")
        return None

    try:
        bdmv_dir = os.path.join(mount_point, "BDMV")
        if not os.path.isdir(bdmv_dir):
            # Try case-insensitive lookup
            for entry in os.listdir(mount_point):
                if entry.upper() == "BDMV":
                    bdmv_dir = os.path.join(mount_point, entry)
                    break

        if not os.path.isdir(bdmv_dir):
            print("  BD disc ID: no BDMV directory found")
            return None

        h = hashlib.sha256()
        files_hashed = 0

        for fname in ("index.bdmv", "MovieObject.bdmv"):
            # Case-insensitive file lookup
            fpath = None
            for entry in os.listdir(bdmv_dir):
                if entry.lower() == fname.lower():
                    fpath = os.path.join(bdmv_dir, entry)
                    break

            if fpath and os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    h.update(f.read())
                files_hashed += 1

        if files_hashed == 0:
            print("  BD disc ID: no BDMV metadata files found")
            return None

        result = h.hexdigest()
        print(f"  BD disc ID: {result[:16]}... ({files_hashed} file(s) hashed)")
        return result
    except Exception as e:
        print(f"  BD disc ID: computation failed: {e}")
        return None
    finally:
        _unmount_disc(mount_point)


def compute_disc_id(media_type: str, device: str) -> str | None:
    """Compute disc ID based on media type. Returns hex string or None."""
    if media_type == "dvd":
        return compute_dvd_disc_id(device)
    elif media_type in ("bd", "uhd"):
        return compute_bd_disc_id(device)
    return None


def main():
    parser = argparse.ArgumentParser(description="Smart video disc ripper")
    parser.add_argument("--drive", type=int, default=0, help="MakeMKV drive ID")
    parser.add_argument("--output", default="/media/archive", help="Archive base directory")
    parser.add_argument("--label", default=None, help="Disc volume label (DRV_LABEL) for directory naming")
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

    # Compute disc ID before ripping (disc is still in drive)
    device = os.environ.get("DEVICE", "/dev/sr1")
    disc_id = compute_disc_id(media_type, device)

    if args.dry_run:
        print("\n[dry-run] Would rip the above titles.")
        return

    content_type = {"tv": "tv", "music": "music"}.get(disc_type, "movies")
    # Use DRV_LABEL (--label) for consistent directory naming, fall back to CINFO disc_label
    dir_label = args.label or disc_label or "unknown_disc"
    label_dir = os.path.join(args.output, content_type, "rips", media_type, dir_label)

    # Auto-increment disc number
    disc_num = 1
    while os.path.exists(os.path.join(label_dir, f"disc{disc_num}")):
        disc_num += 1
    out_dir = os.path.join(label_dir, f"disc{disc_num}")

    print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
    rip_titles(args.drive, to_rip, out_dir)

    # Persist disc ID in output directory
    if disc_id:
        disc_meta = {
            "disc_id": disc_id,
            "media_type": media_type,
            "disc_label": dir_label,
            "disc_number": disc_num,
            "id_type": "dvd_crc64" if media_type == "dvd" else "bd_sha256",
        }
        meta_path = os.path.join(out_dir, ".disc-id.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(disc_meta, f, indent=2)
        except Exception:
            pass

    # Print output dir on a known-format line so auto-rip.sh can capture it
    print(f"STROPHALOS_OUTPUT_DIR={out_dir}")
    print(f"STROPHALOS_DISC_TYPE={disc_type}")
    print(f"STROPHALOS_MEDIA_TYPE={media_type}")
    print(f"STROPHALOS_TITLE_COUNT={len(to_rip)}")
    if disc_id:
        print(f"STROPHALOS_DISC_ID={disc_id}")
    print("Done.")


if __name__ == "__main__":
    main()
