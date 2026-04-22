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


def parse_season_disc(label: str) -> tuple[str, int, int] | None:
    """Extract (series_prefix, season, disc) from labels like MRROBOT_S1D1_NA.

    Matches patterns: PREFIX_S{X}D{Y}, PREFIX_S{X}_D{Y}, with optional trailing suffix.
    Returns None if the label doesn't contain an SxDy pattern.
    """
    m = re.match(r"^(.+?)_S(\d+)_?D(\d+)(?:_.*)?$", label, re.IGNORECASE)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def strip_pressing_code(label: str) -> str | None:
    """Strip trailing disc pressing/mastering codes from a label.

    Publishers embed short codes like UPK75, FPB42, etc. at the end of
    volume labels.  Pattern: 2-5 uppercase letters + 2-3 digits, preceded
    by an underscore or space.  Returns the stripped label, or None if
    nothing was stripped (so callers can skip a redundant retry).
    """
    stripped = re.sub(r"[\s_]+[A-Z]{2,5}\d{2,3}$", "", label)
    return stripped if stripped != label else None
