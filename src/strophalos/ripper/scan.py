"""Disc scanning — makemkvcon output parsing and metadata extraction."""

from __future__ import annotations

import re
import subprocess


def scan_disc(drive_id: int) -> tuple[str | None, dict[int, dict[int, str]], dict[int, str]]:
    """Scan disc and return (disc_label, titles, disc_info).

    titles: {title_id: {attr_id: value}}
    disc_info: {attr_id: value}
    """
    cmd = ["makemkvcon", "-r", "info", f"disc:{drive_id}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    output = result.stdout + result.stderr

    titles: dict[int, dict[int, str]] = {}
    disc_label: str | None = None
    disc_info: dict[int, str] = {}

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


def parse_duration(duration_str: str) -> int:
    """Parse H:MM:SS or M:SS to seconds."""
    parts = duration_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


def get_title_durations(titles: dict[int, dict[int, str]]) -> dict[int, int]:
    """Extract title ID -> duration in seconds. Attribute 9 = duration."""
    durations: dict[int, int] = {}
    for tid, attrs in titles.items():
        if 9 in attrs:
            durations[tid] = parse_duration(attrs[9])
    return durations


def get_title_chapters(titles: dict[int, dict[int, str]]) -> dict[int, int]:
    """Extract title ID -> chapter count. Attribute 8 = chapter count."""
    chapters: dict[int, int] = {}
    for tid, attrs in titles.items():
        if 8 in attrs:
            chapters[tid] = int(attrs[8])
    return chapters


def _parse_size(s: str) -> int:
    """Parse a size string — raw bytes or formatted like '27.3 GB'."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    m = re.match(r"([\d.]+)\s*(TB|GB|MB|KB)", s, re.IGNORECASE)
    if m:
        num = float(m.group(1))
        unit = m.group(2).upper()
        return int(num * {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit])
    return 0


def get_title_sizes(titles: dict[int, dict[int, str]]) -> dict[int, int]:
    """Extract title ID -> output file size in bytes. Attribute 10 = size."""
    sizes: dict[int, int] = {}
    for tid, attrs in titles.items():
        if 10 in attrs:
            sizes[tid] = _parse_size(attrs[10])
    return sizes


def get_title_source_filenames(titles: dict[int, dict[int, str]]) -> dict[int, str]:
    """Extract title ID -> source filename (e.g. '00000.mpls'). Attribute 16."""
    return {tid: attrs[16] for tid, attrs in titles.items() if 16 in attrs}


def get_title_segment_maps(titles: dict[int, dict[int, str]]) -> dict[int, str]:
    """Extract title ID -> raw segment map (e.g. '63' or '13,14'). Attribute 26."""
    return {tid: attrs[26] for tid, attrs in titles.items() if 26 in attrs}


def detect_media_type(disc_info: dict[int, str]) -> str:
    """Detect physical media type: 'dvd', 'bd', or 'uhd'.

    Uses CINFO:1 (disc type string) from the makemkvcon scan.
    """
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
