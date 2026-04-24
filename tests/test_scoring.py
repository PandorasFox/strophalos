"""Tests for episode scoring — time-aligned subtitle similarity comparison."""

from __future__ import annotations

from strophalos.identify.scoring import (
    score_subtitle_similarity,
    tokenize,
)

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
# score_subtitle_similarity
# ---------------------------------------------------------------------------


def _make_cues(texts: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Helper to build cue lists."""
    return texts


class TestScoreSubtitleSimilarity:
    def test_identical_subs(self):
        """Identical cues should produce a high score."""
        cues = [
            (10.0, "Hello there, welcome to the show"),
            (40.0, "The wolf runs through the forest"),
            (70.0, "I need to find the merchant"),
            (100.0, "We should leave before dawn"),
        ]
        score = score_subtitle_similarity(cues, cues)
        assert score > 8.0  # near-perfect match, scaled by 10

    def test_no_overlap(self):
        """Completely different text at same timestamps should score near zero."""
        extracted = [
            (10.0, "alpha bravo charlie delta"),
            (40.0, "echo foxtrot golf hotel"),
        ]
        reference = [
            (10.0, "one two three four"),
            (40.0, "five six seven eight"),
        ]
        score = score_subtitle_similarity(extracted, reference)
        assert score < 0.5

    def test_ocr_noise(self):
        """OCR'd text with character substitutions should still score reasonably."""
        reference = [
            (10.0, "The wolf and the merchant travel north"),
            (40.0, "I will buy your wheat at a fair price"),
            (70.0, "The church controls the northern trade routes"),
        ]
        # Simulated OCR errors: l→I, 0→O, missing chars
        extracted = [
            (10.0, "The woIf and the merchant traveI north"),
            (40.0, "I wiII buy your wheat at a fair price"),
            (70.0, "The church controIs the northern trade routes"),
        ]
        clean_score = score_subtitle_similarity(reference, reference)
        noisy_score = score_subtitle_similarity(extracted, reference)
        # OCR noise should still get a reasonable portion of the clean score
        assert noisy_score > clean_score * 0.5

    def test_timing_offset(self):
        """Shifted cues should still score well due to offset compensation."""
        reference = [
            (30.0, "The wolf runs through the forest"),
            (60.0, "I need to find the merchant guild"),
            (90.0, "We should leave before dawn"),
        ]
        # Same text shifted by +8 seconds (within ±10s compensation)
        shifted = [
            (38.0, "The wolf runs through the forest"),
            (68.0, "I need to find the merchant guild"),
            (98.0, "We should leave before dawn"),
        ]
        aligned_score = score_subtitle_similarity(reference, reference)
        shifted_score = score_subtitle_similarity(shifted, reference)
        # Should recover most of the score via offset compensation
        assert shifted_score > aligned_score * 0.7

    def test_wrong_episode(self):
        """Subs from episode 1 should score much higher against ep1 ref than ep2 ref."""
        ep1_subs = [
            (10.0, "The merchant caravan arrives at dawn"),
            (40.0, "Silver coins for your finest wheat"),
            (70.0, "The northern roads are dangerous"),
        ]
        ep2_ref = [
            (10.0, "The festival begins at midnight"),
            (40.0, "Dancing under the harvest moon"),
            (70.0, "The southern kingdom sends envoys"),
        ]
        score_correct = score_subtitle_similarity(ep1_subs, ep1_subs)
        score_wrong = score_subtitle_similarity(ep1_subs, ep2_ref)
        assert score_correct > score_wrong * 3

    def test_large_timing_offset(self):
        """DVD/whisper timing can be off by minutes — bag-of-words should still match."""
        reference = [
            (10.0, "The merchant caravan arrives at dawn"),
            (40.0, "Silver coins for your finest wheat"),
            (70.0, "The northern roads are dangerous this season"),
        ]
        # Same dialogue but timestamps shifted by 90 seconds (far beyond ±10s compensation)
        shifted = [
            (100.0, "The merchant caravan arrives at dawn"),
            (130.0, "Silver coins for your finest wheat"),
            (160.0, "The northern roads are dangerous this season"),
        ]
        score = score_subtitle_similarity(shifted, reference)
        perfect = score_subtitle_similarity(reference, reference)
        # Bag-of-words should recover the full score despite timing mismatch
        assert score >= perfect * 0.95

    def test_empty_extracted(self):
        ref = [(10.0, "Some text")]
        assert score_subtitle_similarity([], ref) == 0.0

    def test_empty_reference(self):
        ext = [(10.0, "Some text")]
        assert score_subtitle_similarity(ext, []) == 0.0

    def test_both_empty(self):
        assert score_subtitle_similarity([], []) == 0.0
