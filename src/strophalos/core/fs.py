"""Filesystem utilities — filename sanitization, label helpers."""

from __future__ import annotations

import re


def sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a filename."""
    name = name.replace(":", " -")
    name = name.replace("/", " -")
    name = re.sub(r'[?*<>|"\\]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


_WORD_NUMS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def _parse_season_num(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return _WORD_NUMS.get(tok)


def parse_season_disc(label: str) -> tuple[str, int, int] | None:
    """Extract (series_prefix, season, disc) from a disc label.

    Recognizes two forms:
      - Compact volume labels: MRROBOT_S1D1_NA, MR_ROBOT_S2_D3, etc.
      - Natural disc titles:   "Mr. Robot: Season Two (Disc 1)",
                               "BSG Season 3, Disc 2", etc.

    Season/disc numbers may be digits or English words up to twenty.
    Returns None if no season/disc structure is found.
    """
    m = re.match(r"^(.+?)_S(\d+)_?D(\d+)(?:_.*)?$", label, re.IGNORECASE)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))

    m = re.search(
        r"\bSeason\s+(\d+|[A-Za-z]+)\b.*?\bDisc\s+(\d+|[A-Za-z]+)\b",
        label,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        season = _parse_season_num(m.group(1))
        disc = _parse_season_num(m.group(2))
        if season is not None and disc is not None:
            prefix = label[: m.start()].rstrip(" :-,").strip()
            return prefix, season, disc

    return None


def strip_pressing_code(label: str) -> str | None:
    """Strip trailing disc pressing/mastering codes from a label.

    Publishers embed short codes like UPK75, FPB42, etc. at the end of
    volume labels.  Pattern: 2-5 uppercase letters + 2-3 digits, preceded
    by an underscore or space.  Returns the stripped label, or None if
    nothing was stripped (so callers can skip a redundant retry).
    """
    stripped = re.sub(r"[\s_]+[A-Z]{2,5}\d{2,3}$", "", label)
    return stripped if stripped != label else None
