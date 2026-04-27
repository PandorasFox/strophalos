"""Tests for disc classification — movie vs TV vs music scoring."""

from __future__ import annotations

from unittest.mock import patch

from strophalos.ripper.classify import _score_movie, _score_tv, classify_disc

# ---------------------------------------------------------------------------
# _score_movie
# ---------------------------------------------------------------------------


class TestScoreMovie:
    def test_classic_movie_with_extras(self):
        """A 2-hour feature + three short extras → high movie score."""
        sorted_titles = [(0, 7200), (1, 600), (2, 300), (3, 180)]
        score, titles, reason = _score_movie(
            sorted_titles,
            longest_tid=0,
            longest_dur=7200,
            rest=sorted_titles[1:],
            meaningful=[(0, 7200), (1, 600), (2, 300), (3, 180)],
        )
        assert score >= 0.7
        assert titles == [0]
        assert "dominant" in reason or "feature-length" in reason

    def test_single_feature_no_extras(self):
        """A single long title with nothing else."""
        sorted_titles = [(0, 7200)]
        score, titles, reason = _score_movie(
            sorted_titles,
            longest_tid=0,
            longest_dur=7200,
            rest=[],
            meaningful=[(0, 7200)],
        )
        assert score >= 0.2  # at least feature-length bonus
        assert titles == [0]

    def test_many_non_trivial_extras_lower_score(self):
        """Many 10+ minute extras penalize the movie score."""
        sorted_titles = [(0, 7200)] + [(i, 900) for i in range(1, 8)]
        rest = sorted_titles[1:]
        score, titles, reason = _score_movie(
            sorted_titles,
            longest_tid=0,
            longest_dur=7200,
            rest=rest,
            meaningful=sorted_titles,
        )
        # 7 non-trivial extras → lower bonus
        assert score < 0.9


# ---------------------------------------------------------------------------
# _score_tv
# ---------------------------------------------------------------------------


class TestScoreTV:
    def test_uniform_episodes_with_play_all(self):
        """6 uniform 42-min episodes + play-all → high TV score."""
        ep_dur = 2520  # 42 minutes
        # Title 0 is play-all (sum of 6 episodes ≈ 15120)
        play_all = ep_dur * 6
        sorted_titles = [(0, play_all)] + [(i + 1, ep_dur) for i in range(6)]
        rest = sorted_titles[1:]
        meaningful = sorted_titles  # all > 120s

        score, titles, reason = _score_tv(
            sorted_titles,
            longest_tid=0,
            longest_dur=play_all,
            rest=rest,
            meaningful=meaningful,
        )
        assert score >= 0.8
        assert "play-all" in reason
        assert "episodes" in reason
        # Play-all title should NOT be in the rip list (it's not episode-length)
        assert 0 not in titles
        assert len(titles) == 6

    def test_three_varied_episodes(self):
        """3 episodes with some variation — should still score as TV."""
        sorted_titles = [(0, 3000), (1, 2800), (2, 2600)]
        score, titles, reason = _score_tv(
            sorted_titles,
            longest_tid=0,
            longest_dur=3000,
            rest=sorted_titles[1:],
            meaningful=sorted_titles,
        )
        assert score > 0.0
        assert "episodes" in reason
        assert len(titles) >= 2

    def test_too_few_candidates(self):
        """Only 1 episode candidate → returns 0."""
        sorted_titles = [(0, 7200), (1, 2500)]
        score, titles, reason = _score_tv(
            sorted_titles,
            longest_tid=0,
            longest_dur=7200,
            rest=sorted_titles[1:],
            meaningful=sorted_titles,
        )
        assert score == 0.0
        assert "no episode duration cluster" in reason

    def test_very_uniform_episodes(self):
        """8 episodes all exactly the same length → very high uniformity bonus."""
        ep_dur = 2520
        sorted_titles = [(i, ep_dur) for i in range(8)]
        score, titles, reason = _score_tv(
            sorted_titles,
            longest_tid=0,
            longest_dur=ep_dur,
            rest=sorted_titles[1:],
            meaningful=sorted_titles,
        )
        assert score >= 0.7
        assert "very uniform" in reason


# ---------------------------------------------------------------------------
# classify_disc (needs mocked backends)
# ---------------------------------------------------------------------------


class TestClassifyDisc:
    """Test classify_disc with mocked MusicBrainz and TMDb backends."""

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_empty_disc(self, mock_tmdb, mock_mb):
        disc_type, titles, reason, _meta = classify_disc({}, {})
        assert disc_type == "unknown"
        assert titles == []

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_single_long_title_is_movie(self, mock_tmdb, mock_mb):
        durations = {0: 7200}
        disc_type, titles, reason, _meta = classify_disc(durations, {})
        assert disc_type == "movie"
        assert titles == [0]

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_movie_disc_pattern(self, mock_tmdb, mock_mb):
        """Feature + short extras → movie."""
        durations = {0: 7200, 1: 300, 2: 180, 3: 120}
        disc_type, titles, reason, _meta = classify_disc(durations, {})
        assert disc_type == "movie"
        assert 0 in titles

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_tv_disc_pattern(self, mock_tmdb, mock_mb):
        """6 uniform episodes + play-all → tv."""
        ep_dur = 2520
        play_all = ep_dur * 6
        durations = {0: play_all}
        durations.update({i + 1: ep_dur for i in range(6)})
        disc_type, titles, reason, _meta = classify_disc(durations, {})
        assert disc_type == "tv"
        # Should rip episodes, not play-all
        assert 0 not in titles

    @patch("strophalos.ripper.classify.score_musicbrainz")
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_strong_musicbrainz_match_is_music(self, mock_tmdb, mock_mb):
        """Strong MusicBrainz match → music, returns play-all title."""
        mock_mb.return_value = (0.9, {"artist": "Test", "title": "Album", "track_count": 12})
        durations = {i: 240 for i in range(12)}
        chapters = {i: 1 for i in range(12)}
        # Title 12 is the play-all with 12 chapters
        durations[12] = 240 * 12
        chapters[12] = 12
        disc_type, titles, reason, meta = classify_disc(durations, chapters, "TEST_ALBUM")
        assert disc_type == "music"
        assert titles == [12]  # play-all title selected
        assert meta is not None

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.3))
    def test_tmdb_tv_boost(self, mock_tmdb, mock_mb):
        """TMDb TV boost should tip a borderline disc toward TV."""
        # Borderline case: 4 episodes of 2500s + a 5000s title
        durations = {0: 5000, 1: 2500, 2: 2500, 3: 2500, 4: 2500}
        disc_type, titles, reason, _meta = classify_disc(durations, {}, "SOME_SHOW")
        assert disc_type == "tv"

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_box_set_volume_label_forces_tv(self, mock_tmdb, mock_mb):
        """Compact volume labels (MRROBOT_S2D1_NA) force TV."""
        durations = {0: 3100, 1: 2800, 2: 2700, 3: 2600}
        disc_type, titles, reason, _meta = classify_disc(durations, {}, "MRROBOT_S2D1_NA")
        assert disc_type == "tv"
        assert "box-set label" in reason

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_box_set_pretty_title_forces_tv(self, mock_tmdb, mock_mb):
        """Natural-form disc titles ("Mr. Robot: Season Two (Disc 1)") force TV —
        regression for the actual MakeMKV-reported label on Mr. Robot S2D1."""
        # Mirrors the observed MR_ROBOT S2D1 title shape: long main title +
        # two episode-length titles + short extras. Without the label boost,
        # movie and tv tie at ~0.40 and movie wins by a hair.
        durations = {0: 4937, 1: 3781, 2: 3921, 3: 163, 4: 192, 5: 521}
        disc_type, titles, reason, _meta = classify_disc(durations, {}, "Mr. Robot: Season Two (Disc 1)")
        assert disc_type == "tv"
        assert "box-set label" in reason

    @patch("strophalos.ripper.classify.score_musicbrainz", return_value=(0.0, None))
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_non_box_set_label_unaffected(self, mock_tmdb, mock_mb):
        """Labels without SxDy structure shouldn't get the TV boost."""
        durations = {0: 7200, 1: 300, 2: 180}
        disc_type, titles, reason, _meta = classify_disc(durations, {}, "SOME_MOVIE_2020")
        assert disc_type == "movie"
        assert "box-set label" not in reason


# ---------------------------------------------------------------------------
# dedup_segment_variants
# ---------------------------------------------------------------------------


class TestDedupSegmentVariants:
    def _parse_scan(self, path):
        """Replay scan.py's TINFO regex over a saved makemkvcon info dump."""
        import re

        titles: dict[int, dict[int, str]] = {}
        for ln in open(path).read().splitlines():
            m = re.match(r'TINFO:(\d+),(\d+),\d+,"(.+)"', ln)
            if m:
                tid, attr, val = int(m.group(1)), int(m.group(2)), m.group(3)
                titles.setdefault(tid, {})[attr] = val
        return titles

    def test_bsg_s1_d2_collapses_mpls_m2ts_pairs(self):
        """BSG S1 D2: 5 .mpls episodes + 5 .m2ts duplicates + 2 uniques → 7 keepers."""
        from pathlib import Path

        from strophalos.ripper.dedup import dedup_segment_variants

        scan_path = Path(__file__).parent / "data" / "bsg_s1_d2_scan.txt"
        titles = self._parse_scan(scan_path)
        filtered, dropped = dedup_segment_variants(titles)

        # Keep the .mpls variants (0-4) plus uniques (5, 6).
        assert set(filtered.keys()) == {0, 1, 2, 3, 4, 5, 6}
        # Drop the .m2ts raw-stream siblings (7-11).
        dropped_tids = {d for d, _ in dropped}
        assert dropped_tids == {7, 8, 9, 10, 11}
        # Each drop should point at its .mpls counterpart (same segment).
        drop_map = dict(dropped)
        assert drop_map[7] == 0  # segment 63
        assert drop_map[11] == 4  # segment 68

    def test_noop_when_no_segment_map(self):
        """DVD-style titles (no attr 26) pass through unchanged."""
        from strophalos.ripper.dedup import dedup_segment_variants

        titles = {0: {9: "0:30:00"}, 1: {9: "0:30:00"}}
        filtered, dropped = dedup_segment_variants(titles)
        assert filtered == titles
        assert dropped == []

    def test_prefers_lowest_tid_when_all_same_type(self):
        """Two .m2ts titles with the same segment set → keep the lower id."""
        from strophalos.ripper.dedup import dedup_segment_variants

        titles = {
            3: {16: "00063.m2ts", 26: "63"},
            7: {16: "00063.m2ts", 26: "63"},
        }
        filtered, dropped = dedup_segment_variants(titles)
        assert set(filtered.keys()) == {3}
        assert dropped == [(7, 3)]

    def test_multi_segment_grouping(self):
        """Segment sets are compared unordered; {13,14} matches {14,13}."""
        from strophalos.ripper.dedup import dedup_segment_variants

        titles = {
            0: {16: "00006.mpls", 26: "13,14"},
            1: {16: "00006.m2ts", 26: "14,13"},
        }
        filtered, dropped = dedup_segment_variants(titles)
        assert set(filtered.keys()) == {0}
        assert dropped == [(1, 0)]


class TestFilterBitrateOutliers:
    def test_bsg_s1_d2_drops_featurette(self):
        """BSG T6 (~5 Mbps) should drop against 5 episodes (~23 Mbps)."""
        import re
        from pathlib import Path

        from strophalos.ripper.dedup import dedup_segment_variants, filter_bitrate_outliers

        titles: dict[int, dict[int, str]] = {}
        scan_path = Path(__file__).parent / "data" / "bsg_s1_d2_scan.txt"
        for ln in open(scan_path).read().splitlines():
            m = re.match(r'TINFO:(\d+),(\d+),\d+,"(.+)"', ln)
            if m:
                tid, attr, val = int(m.group(1)), int(m.group(2)), m.group(3)
                titles.setdefault(tid, {})[attr] = val

        # Run the full pipeline: segment dedup → bitrate filter.
        titles, _ = dedup_segment_variants(titles)
        filtered, dropped = filter_bitrate_outliers(titles)

        # Episodes survive; T5 (low duration but missing size attr path is fine)
        # and T6 (low bitrate) are evaluated.  T6 must be in the drop set.
        dropped_tids = {d[0] for d in dropped}
        assert 6 in dropped_tids, f"expected T6 in drops, got {dropped_tids}"
        # Episodes 0-4 must be kept.
        assert {0, 1, 2, 3, 4}.issubset(filtered.keys())
        # The reported median should be in a sensible BD range (15-30 Mbps).
        _, _, median_mbps = dropped[0]
        assert 15.0 <= median_mbps <= 30.0

    def test_noop_when_too_few_titles(self):
        """Fewer than 3 titles with bitrate data → skip the filter."""
        from strophalos.ripper.dedup import filter_bitrate_outliers

        titles = {
            0: {9: "0:45:00", 10: "7.5 GB"},
            1: {9: "0:10:00", 10: "0.2 GB"},
        }
        filtered, dropped = filter_bitrate_outliers(titles)
        assert filtered == titles
        assert dropped == []

    def test_keeps_titles_missing_size_or_duration(self):
        """A title with no size attr can't be judged; keep it."""
        from strophalos.ripper.dedup import filter_bitrate_outliers

        titles = {
            0: {9: "0:45:00", 10: "7.5 GB"},
            1: {9: "0:45:00", 10: "7.5 GB"},
            2: {9: "0:45:00", 10: "7.5 GB"},
            3: {9: "0:45:00"},  # no size
        }
        filtered, _ = filter_bitrate_outliers(titles)
        assert 3 in filtered

    def test_drops_clear_outlier(self):
        """Four 8GB/45min episodes + one 1GB/45min extra → extra drops."""
        from strophalos.ripper.dedup import filter_bitrate_outliers

        titles = {
            0: {9: "0:45:00", 10: "8.0 GB"},
            1: {9: "0:45:00", 10: "8.0 GB"},
            2: {9: "0:45:00", 10: "8.0 GB"},
            3: {9: "0:45:00", 10: "8.0 GB"},
            4: {9: "0:45:00", 10: "1.0 GB"},
        }
        filtered, dropped = filter_bitrate_outliers(titles)
        assert set(filtered.keys()) == {0, 1, 2, 3}
        assert [d[0] for d in dropped] == [4]

    def test_double_length_finale_doesnt_hide_featurette(self):
        """BSG S2D5 t7 regression: a double-length finale must not push the
        cluster anchor so high that an episode-length low-bitrate featurette
        falls below 0.5× and escapes evaluation."""
        from strophalos.ripper.dedup import filter_bitrate_outliers

        titles = {
            # 2 regular episodes ~43 min, ~10.8 GB (~33 Mbps)
            0: {9: "0:43:21", 10: "10.8 GB"},
            1: {9: "0:43:45", 10: "10.9 GB"},
            # Double-length finale 68 min, 17 GB — same bitrate, wider runtime.
            2: {9: "1:07:54", 10: "17.0 GB"},
            # 28-min featurette at ~5 Mbps — must drop. Lives between
            # 0.5× and 2.0× of the *median* episode duration (43m) but is
            # below 0.5× the max (68m).
            7: {9: "0:27:37", 10: "1.0 GB"},
            # Short menu/stinger noise, excluded by the 600s floor.
            12: {9: "0:02:51", 10: "0.1 GB"},
            13: {9: "0:02:27", 10: "0.1 GB"},
        }
        filtered, dropped = filter_bitrate_outliers(titles)
        dropped_tids = {d[0] for d in dropped}
        assert 7 in dropped_tids, f"t7 featurette must drop, got {dropped_tids}"
        assert {0, 1, 2}.issubset(filtered.keys())
        assert {12, 13}.issubset(filtered.keys())

    def test_short_low_bitrate_noise_doesnt_hide_featurette(self):
        """BSG S2D2 t10 regression: episode-length featurette at low bitrate
        must still drop even when the disc has several short low-bitrate
        titles that would skew a naive all-titles median downward."""
        from strophalos.ripper.dedup import filter_bitrate_outliers

        titles = {
            # 5 real episodes: ~23 Mbps
            0: {9: "0:45:00", 10: "7.5 GB"},
            1: {9: "0:45:00", 10: "7.5 GB"},
            2: {9: "0:45:00", 10: "7.5 GB"},
            3: {9: "0:45:00", 10: "7.5 GB"},
            4: {9: "0:45:00", 10: "7.5 GB"},
            # Episode-duration featurette at ~3 Mbps — must drop.
            10: {9: "0:45:00", 10: "1.0 GB"},
            # Short low-bitrate menu/stinger noise — must NOT drag median down.
            20: {9: "0:01:00", 10: "0.05 GB"},
            21: {9: "0:02:00", 10: "0.08 GB"},
            22: {9: "0:00:30", 10: "0.02 GB"},
            23: {9: "0:01:30", 10: "0.04 GB"},
        }
        filtered, dropped = filter_bitrate_outliers(titles)
        dropped_tids = {d[0] for d in dropped}
        assert 10 in dropped_tids, f"t10 featurette must drop, got {dropped_tids}"
        # Short non-cluster titles pass through — duration-based classification
        # handles them; we don't want this filter to double up on that job.
        assert {20, 21, 22, 23}.issubset(filtered.keys())
        assert {0, 1, 2, 3, 4}.issubset(filtered.keys())
