"""Filesystem utilities — filename sanitization, hard-link helpers."""

from __future__ import annotations

import re


def sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a filename."""
    name = name.replace(":", " -")
    name = name.replace("/", " -")
    name = re.sub(r'[?*<>|"\\]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name
