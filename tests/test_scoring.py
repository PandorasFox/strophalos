"""Tests for episode title scoring — word weighting, phrase matching, position decay."""

from __future__ import annotations

from strophalos.identify.scoring import (
    compute_word_weights,
    position_weight,
    score_match,
    tokenize,
)
from strophalos.types import Episode

# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic(self):
        assert tokenize("Hello World") == ["hello", "world"]

    def test_numbers(self):
        assert tokenize("Episode 3") == ["episode", "3"]

    def test_punctuation_stripped(self):
        assert tokenize("Wolf's Rain — Part 2!") == ["wolf", "s", "rain", "part", "2"]

    def test_empty(self):
        assert tokenize("") == []

    def test_only_punctuation(self):
        assert tokenize("!@#$%") == []

    def test_mixed_case(self):
        assert tokenize("The ONE Where Ross Gets HIGH") == ["the", "one", "where", "ross", "gets", "high"]


# ---------------------------------------------------------------------------
# compute_word_weights
# ---------------------------------------------------------------------------


class TestComputeWordWeights:
    def test_empty(self):
        assert compute_word_weights([]) == {}

    def test_all_unique(self):
        episodes = [
            Episode(1, 1, "Wolf and Best Clothes", 0),
            Episode(1, 2, "Rain and Distant Past", 0),
        ]
        weights = compute_word_weights(episodes)
        # "and" appears in both (2/2 = 100% > 50%) → low weight
        assert weights["and"] == 0.1
        # "wolf" appears in 1/2 = 50%, not > 50% → full weight
        assert weights["wolf"] == 1.0
        assert weights["rain"] == 1.0

    def test_series_word_downweighted(self):
        """A word appearing in >50% of episode titles should be downweighted."""
        episodes = [
            Episode(1, 1, "The One Where Ross Gets High", 0),
            Episode(1, 2, "The One Where Rachel Smokes", 0),
            Episode(1, 3, "The One with the Holiday Armadillo", 0),
        ]
        weights = compute_word_weights(episodes)
        # "the", "one" appear in all 3 → downweighted
        assert weights["the"] == 0.1
        assert weights["one"] == 0.1
        # "ross" appears in 1/3 → full weight
        assert weights["ross"] == 1.0
        # "where" appears in 2/3 → downweighted
        assert weights["where"] == 0.1

    def test_single_episode(self):
        """With one episode, every word appears in 100% → all downweighted."""
        episodes = [Episode(1, 1, "Pilot Episode", 0)]
        weights = compute_word_weights(episodes)
        assert weights["pilot"] == 0.1
        assert weights["episode"] == 0.1


# ---------------------------------------------------------------------------
# position_weight
# ---------------------------------------------------------------------------


class TestPositionWeight:
    def test_zero_duration(self):
        assert position_weight(0.0, 0.0) == 1.0

    def test_very_start(self):
        # t=0 should be 1.5x
        assert position_weight(0.0, 1000.0) == 1.5

    def test_at_20_percent(self):
        # At exactly 20%, should be 1.0 (taper ends)
        assert position_weight(200.0, 1000.0) == 1.0

    def test_at_10_percent(self):
        # Halfway through taper: 1.0 + 0.5 * (1.0 - 0.5) = 1.25
        assert position_weight(100.0, 1000.0) == 1.25

    def test_middle(self):
        # Between 20% and 75% → flat 1.0
        assert position_weight(500.0, 1000.0) == 1.0

    def test_late(self):
        # >= 75% → 0.3
        assert position_weight(800.0, 1000.0) == 0.3

    def test_exactly_75_percent(self):
        assert position_weight(750.0, 1000.0) == 0.3


# ---------------------------------------------------------------------------
# score_match
# ---------------------------------------------------------------------------


class TestScoreMatch:
    def _ep(self, title: str) -> Episode:
        return Episode(1, 1, title, runtime_seconds=1400)

    def test_no_title_words(self):
        assert score_match(self._ep(""), [], {}, 1400) == 0.0

    def test_no_subtitle_text(self):
        assert score_match(self._ep("Some Title"), [], {}, 1400) == 0.0

    def test_phrase_match_bonus(self):
        """All discriminating words in a single cue should trigger phrase bonus."""
        ep = self._ep("The One Where Ross Gets High")
        weights = {"the": 0.1, "one": 0.1, "where": 0.1, "ross": 1.0, "gets": 1.0, "high": 1.0}
        # Phrase match: all disc words (ross, gets, high) in one cue
        subs = [(100.0, "Ross gets high at the party")]
        score = score_match(ep, subs, weights, 1400)
        # Should include phrase bonus (5.0 * position_weight) + word scores
        assert score > 5.0

    def test_no_phrase_match_lower_score(self):
        """Words scattered across cues should score lower than a phrase match."""
        ep = self._ep("The One Where Ross Gets High")
        weights = {"the": 0.1, "one": 0.1, "where": 0.1, "ross": 1.0, "gets": 1.0, "high": 1.0}
        # Phrase match in one cue
        phrase_subs = [(100.0, "Ross gets high at the party")]
        # Words in separate cues (no phrase match)
        scattered_subs = [(100.0, "Ross enters the room"), (200.0, "He gets the cookie"), (300.0, "Feeling high")]
        phrase_score = score_match(ep, phrase_subs, weights, 1400)
        scattered_score = score_match(ep, scattered_subs, weights, 1400)
        assert phrase_score > scattered_score

    def test_common_words_downweighted(self):
        """Common words (weight=0.1) should contribute less than discriminating ones."""
        ep = self._ep("the big test")
        # "the" is common, "big" and "test" are discriminating
        weights = {"the": 0.1, "big": 1.0, "test": 1.0}
        subs = [(100.0, "the big test is today")]
        score = score_match(ep, subs, weights, 1400)
        # The phrase bonus uses disc_words (big, test) — both present in one cue
        # Word scores: the=0.1*pw, big=1.0*pw, test=1.0*pw
        assert score > 5.0  # phrase bonus fires

    def test_early_cue_scores_higher(self):
        """A word appearing early in the file should score higher than appearing late."""
        ep = self._ep("target")
        weights = {"target": 1.0}
        early_subs = [(50.0, "target acquired")]  # ~3.5% into a 1400s file → pw ≈ 1.41
        late_subs = [(1200.0, "target acquired")]  # ~85% → pw = 0.3
        early_score = score_match(ep, early_subs, weights, 1400)
        late_score = score_match(ep, late_subs, weights, 1400)
        assert early_score > late_score
