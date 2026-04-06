"""Whisper transcription — stub for external Whisper container.

The Whisper service runs in a standalone container. This module provides the
interface stub; the actual integration will be connected later.
"""

from __future__ import annotations

import os
from pathlib import Path


def transcribe(
    mkv_path: Path,
    duration: float,
    *,
    url: str | None = None,
    model: str = "base",
    language: str = "en",
    cutoff_fraction: float = 0.25,
) -> list[tuple[float, str]]:
    """Transcribe audio from an MKV file via the external Whisper service.

    Returns (timestamp_seconds, text) pairs filtered to the first
    cutoff_fraction of the file's duration.

    Returns [] if the Whisper service is not configured.
    """
    whisper_url = url or os.environ.get("WHISPER_URL", "")
    if not whisper_url:
        return []

    # TODO: connect to external Whisper container
    # 1. Extract audio track from MKV (ffmpeg -i mkv -f wav pipe:1)
    # 2. POST audio to whisper_url with model/language params
    # 3. Parse timestamped segments from response
    # 4. Filter to first cutoff_fraction of duration
    return []
