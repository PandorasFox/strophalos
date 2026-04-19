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


def strip_pressing_code(label: str) -> str | None:
    """Strip trailing disc pressing/mastering codes from a label.

    Publishers embed short codes like UPK75, FPB42, etc. at the end of
    volume labels.  Pattern: 2-5 uppercase letters + 2-3 digits, preceded
    by an underscore or space.  Returns the stripped label, or None if
    nothing was stripped (so callers can skip a redundant retry).
    """
    stripped = re.sub(r"[\s_]+[A-Z]{2,5}\d{2,3}$", "", label)
    return stripped if stripped != label else None
