"""Shared data types used across the identification and ripping pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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


@dataclass
class MatchResult:
    """Result of identifying a single file."""

    file: RippedFile
    episode: Episode
    method: str  # "opensubtitles hash", "anidb hash", "subtitle match", "duration", "forward order"
    score: float = 0.0


@dataclass
class SubtitleTrack:
    track_id: int
    codec: str
    language: str
    name: str
    is_text: bool
