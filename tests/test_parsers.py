"""Tests for parsers and transforms — duration, size, SRT, disc labels, filenames."""

from __future__ import annotations

from strophalos.backends.tmdb import clean_movie_label
from strophalos.core.fs import sanitize_filename
from strophalos.identify.subtitles import parse_srt
from strophalos.ripper.scan import (
    _parse_size,
    detect_media_type,
    get_title_chapters,
    get_title_durations,
    get_title_sizes,
    parse_duration,
)

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_hms(self):
        assert parse_duration("1:30:00") == 5400

    def test_ms(self):
        assert parse_duration("42:30") == 2550

    def test_zero(self):
        assert parse_duration("0:00:00") == 0

    def test_short(self):
        assert parse_duration("0:02:30") == 150

    def test_garbage(self):
        assert parse_duration("nope") == 0

    def test_single_value(self):
        assert parse_duration("100") == 0


# ---------------------------------------------------------------------------
# _parse_size
# ---------------------------------------------------------------------------


class TestParseSize:
    def test_raw_bytes(self):
        assert _parse_size("1048576") == 1048576

    def test_gb(self):
        assert _parse_size("27.3 GB") == int(27.3 * 1024**3)

    def test_mb(self):
        assert _parse_size("500 MB") == 500 * 1024**2

    def test_kb(self):
        assert _parse_size("1024 KB") == 1024 * 1024

    def test_tb(self):
        assert _parse_size("1.5 TB") == int(1.5 * 1024**4)

    def test_empty(self):
        assert _parse_size("") == 0

    def test_garbage(self):
        assert _parse_size("unknown") == 0

    def test_whitespace(self):
        assert _parse_size("  27.3 GB  ") == int(27.3 * 1024**3)


# ---------------------------------------------------------------------------
# detect_media_type
# ---------------------------------------------------------------------------


class TestDetectMediaType:
    def test_dvd(self):
        assert detect_media_type({1: "DVD disc"}) == "dvd"

    def test_bluray(self):
        assert detect_media_type({1: "Blu-ray disc"}) == "bd"

    def test_uhd_aacs_v2(self):
        assert detect_media_type({1: "Blu-ray disc (AACS v2)"}) == "uhd"

    def test_uhd_keyword(self):
        assert detect_media_type({1: "UHD Blu-ray"}) == "uhd"

    def test_empty(self):
        assert detect_media_type({}) == "bd"  # default fallback

    def test_unknown_string(self):
        assert detect_media_type({1: "something weird"}) == "bd"


# ---------------------------------------------------------------------------
# get_title_durations / chapters / sizes
# ---------------------------------------------------------------------------


class TestTitleExtraction:
    def test_durations(self):
        titles = {
            0: {9: "1:30:00", 8: "12"},
            1: {9: "0:42:30"},
        }
        durs = get_title_durations(titles)
        assert durs == {0: 5400, 1: 2550}

    def test_chapters(self):
        titles = {
            0: {8: "12", 9: "1:00:00"},
            1: {8: "6"},
        }
        chapters = get_title_chapters(titles)
        assert chapters == {0: 12, 1: 6}

    def test_sizes(self):
        titles = {
            0: {10: "27.3 GB"},
            1: {10: "1048576"},
        }
        sizes = get_title_sizes(titles)
        assert sizes[0] == int(27.3 * 1024**3)
        assert sizes[1] == 1048576

    def test_missing_attrs(self):
        titles = {0: {1: "some title"}}
        assert get_title_durations(titles) == {}
        assert get_title_chapters(titles) == {}
        assert get_title_sizes(titles) == {}


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_colon(self):
        assert sanitize_filename("Movie: The Sequel") == "Movie - The Sequel"

    def test_special_chars(self):
        assert sanitize_filename('What? Why! <No> "Way" |*|') == "What Why! No Way"

    def test_whitespace_collapse(self):
        assert sanitize_filename("  too   many   spaces  ") == "too many spaces"

    def test_already_clean(self):
        assert sanitize_filename("Normal Title") == "Normal Title"

    def test_empty(self):
        assert sanitize_filename("") == ""

    def test_backslash(self):
        assert sanitize_filename("path\\to\\file") == "pathtofile"


# ---------------------------------------------------------------------------
# clean_movie_label
# ---------------------------------------------------------------------------


class TestCleanMovieLabel:
    def test_underscores(self):
        assert clean_movie_label("THE_MATRIX") == "THE MATRIX"

    def test_strip_bd(self):
        assert clean_movie_label("MOVIE BD") == "MOVIE"

    def test_strip_dvd(self):
        assert clean_movie_label("MOVIE DVD") == "MOVIE"

    def test_strip_disc_number(self):
        assert clean_movie_label("MOVIE DISC 2") == "MOVIE"

    def test_keeps_non_disc_suffixes(self):
        """Should NOT strip S1/D1 patterns — those aren't disc format labels."""
        assert clean_movie_label("MOVIE_S1") == "MOVIE S1"


# ---------------------------------------------------------------------------
# parse_srt
# ---------------------------------------------------------------------------


class TestParseSrt:
    def test_basic_srt(self):
        srt = "1\n00:01:30,500 --> 00:01:33,000\nHello World\n\n2\n00:02:00,000 --> 00:02:03,500\nSecond line\n\n"
        results = parse_srt(srt)
        assert len(results) == 2
        assert results[0] == (90.5, "Hello World")
        assert results[1] == (120.0, "Second line")

    def test_multiline_cue(self):
        srt = "1\n00:00:10,000 --> 00:00:15,000\nLine one\nLine two\n\n"
        results = parse_srt(srt)
        assert len(results) == 1
        assert results[0][1] == "Line one Line two"

    def test_html_tags_stripped(self):
        srt = "1\n00:00:05,000 --> 00:00:10,000\n<i>Italic text</i>\n\n"
        results = parse_srt(srt)
        assert results[0][1] == "Italic text"

    def test_empty_srt(self):
        assert parse_srt("") == []
