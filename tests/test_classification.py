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
        assert "too few" in reason

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
