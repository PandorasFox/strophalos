"""Tests for episode_duration_cluster — the shared duration-cluster helper."""

from __future__ import annotations

from strophalos.ripper.cluster import episode_duration_cluster


def _mins(m: int) -> int:
    return m * 60


class TestEpisodeDurationCluster:
    def test_empty_input(self):
        tids, median = episode_duration_cluster({})
        assert tids == []
        assert median == 0

    def test_all_below_floor(self):
        """Sub-600s titles are excluded from candidates entirely."""
        durations = {0: 60, 1: 300, 2: 599}
        tids, median = episode_duration_cluster(durations)
        assert tids == []
        assert median == 0

    def test_uniform_episode_disc(self):
        """4 × ~43min episodes — all in cluster, median matches."""
        durations = {0: _mins(43), 1: _mins(43), 2: _mins(44), 3: _mins(43)}
        tids, median = episode_duration_cluster(durations)
        assert tids == [0, 1, 2, 3]
        assert median == _mins(43)

    def test_double_length_finale_keeps_short_featurette(self):
        """BSG S2D5 shape: 68min finale doesn't push 28min out of band.

        Median anchors on the middle candidate (43min), band is [21.5, 86],
        so the 28min featurette stays in the cluster and gets considered by
        the downstream bitrate filter.  This is the exact failure mode that
        an earlier max-anchored cluster had.
        """
        durations = {
            0: _mins(43),
            1: _mins(44),
            2: _mins(68),
            7: _mins(28),
        }
        tids, median = episode_duration_cluster(durations)
        assert set(tids) == {0, 1, 2, 7}
        assert median == _mins(44)  # middle of sorted [28, 43, 44, 68]

    def test_play_all_excluded_from_cluster(self):
        """5 × 43min + 1 × 215min play-all: cluster is the 5 episodes."""
        durations = {
            0: _mins(43),
            1: _mins(43),
            2: _mins(43),
            3: _mins(43),
            4: _mins(43),
            5: _mins(215),
        }
        tids, median = episode_duration_cluster(durations)
        assert tids == [0, 1, 2, 3, 4]
        assert median == _mins(43)

    def test_short_titles_below_floor_are_ignored_for_median(self):
        """2min stingers don't pull the median down even in the input dict."""
        durations = {
            0: _mins(43),
            1: _mins(44),
            2: _mins(43),
            10: 120,  # below 600s floor
            11: 30,
        }
        tids, median = episode_duration_cluster(durations)
        assert tids == [0, 1, 2]
        assert median == _mins(43)

    def test_out_of_band_candidate_excluded(self):
        """A candidate >= floor but outside [0.5, 2.0]× median stays out."""
        durations = {
            0: _mins(43),
            1: _mins(43),
            2: _mins(43),
            3: _mins(20),  # >= 600s floor (1200s), but < 0.5 × 43min (21.5min)
        }
        tids, median = episode_duration_cluster(durations)
        assert tids == [0, 1, 2]
        assert median == _mins(43)

    def test_custom_thresholds(self):
        """Floor and band are overridable for callers with different needs."""
        durations = {0: 300, 1: 310, 2: 305, 3: 20}
        tids, median = episode_duration_cluster(durations, floor_sec=120)
        assert tids == [0, 1, 2]
        assert median == 305
