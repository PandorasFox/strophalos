"""Shared result types for rip operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RipResult:
    """Structured output from a rip operation."""

    output_dir: str
    disc_type: str  # "tv" | "movie" | "music" | "data"
    media_type: str  # "dvd" | "bd" | "uhd" | "cd" | "data"
    title_count: int
    disc_id: str | None = None
    label: str = ""
    # Music BD metadata (set by rip_video when disc_type == "music")
    mb_metadata: dict | None = None
