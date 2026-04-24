"""SRT subtitle parser — shared by subtitle extraction and whisper backend."""

from __future__ import annotations

import re


def parse_srt(srt_text: str) -> list[tuple[float, str]]:
    """Parse SRT format into (timestamp_seconds, text) pairs."""
    results: list[tuple[float, str]] = []
    for match in re.finditer(r"(\d+):(\d+):([\d,]+)\s*-->.*?\n(.*?)(?:\n\n|\Z)", srt_text, re.DOTALL):
        h, m = int(match.group(1)), int(match.group(2))
        s = float(match.group(3).replace(",", "."))
        timestamp = h * 3600 + m * 60 + s
        line = re.sub(r"<[^>]+>", "", match.group(4)).strip().replace("\n", " ")
        if line:
            results.append((timestamp, line))
    return results
