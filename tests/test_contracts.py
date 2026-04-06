"""Contract tests — verify the interfaces between shell and Python layers."""

from __future__ import annotations

import re
from pathlib import Path

from strophalos.cli.identify_episodes import _format_ranges, _format_run
from strophalos.types import Episode, MatchResult, RippedFile

# ---------------------------------------------------------------------------
# Notification range formatting
# ---------------------------------------------------------------------------


class TestFormatRanges:
    def _mr(self, season: int, episode: int) -> MatchResult:
        ep = Episode(season, episode, "", 0)
        rf = RippedFile(path=Path("x.mkv"), duration_seconds=0)
        return MatchResult(file=rf, episode=ep, method="test")

    def test_single(self):
        assert _format_ranges([self._mr(1, 1)]) == "S01E01"

    def test_contiguous(self):
        mrs = [self._mr(1, 1), self._mr(1, 2), self._mr(1, 3)]
        assert _format_ranges(mrs) == "S01E01–E03"

    def test_gap(self):
        mrs = [self._mr(1, 1), self._mr(1, 3)]
        assert _format_ranges(mrs) == "S01E01, S01E03"

    def test_cross_season(self):
        mrs = [self._mr(1, 12), self._mr(2, 1)]
        result = _format_ranges(mrs)
        assert "S01E12" in result
        assert "S02E01" in result

    def test_empty(self):
        assert _format_ranges([]) == ""

    def test_mixed_contiguous_and_gap(self):
        mrs = [self._mr(1, 1), self._mr(1, 2), self._mr(1, 5), self._mr(1, 6)]
        result = _format_ranges(mrs)
        assert "S01E01–E02" in result
        assert "S01E05–E06" in result


class TestFormatRun:
    def test_single(self):
        assert _format_run((1, 1), (1, 1)) == "S01E01"

    def test_same_season(self):
        assert _format_run((1, 1), (1, 5)) == "S01E01–E05"

    def test_cross_season(self):
        assert _format_run((1, 12), (2, 3)) == "S01E12–S02E03"


# ---------------------------------------------------------------------------
# STROPHALOS_* output protocol
# ---------------------------------------------------------------------------


class TestStrophalosProtocol:
    """Verify the output line format that auto-rip.sh parses."""

    def test_output_lines_parseable(self):
        """The STROPHALOS_* lines should be parseable by grep + cut."""
        lines = [
            "STROPHALOS_OUTPUT_DIR=/media/archive/tv/rips/bd/SHOW/disc1",
            "STROPHALOS_DISC_TYPE=tv",
            "STROPHALOS_MEDIA_TYPE=bd",
            "STROPHALOS_TITLE_COUNT=6",
            "STROPHALOS_DISC_ID=abc123",
        ]
        for line in lines:
            # Must match: starts with STROPHALOS_, has =, value after =
            assert re.match(r"^STROPHALOS_\w+=.+$", line), f"Bad format: {line}"
            key, value = line.split("=", 1)
            assert key.startswith("STROPHALOS_")
            assert len(value) > 0


# ---------------------------------------------------------------------------
# Episode.code property
# ---------------------------------------------------------------------------


class TestEpisodeCode:
    def test_basic(self):
        assert Episode(1, 5, "Test", 0).code == "S01E05"

    def test_padding(self):
        assert Episode(12, 99, "Test", 0).code == "S12E99"

    def test_season_zero(self):
        assert Episode(0, 1, "Special", 0).code == "S00E01"
