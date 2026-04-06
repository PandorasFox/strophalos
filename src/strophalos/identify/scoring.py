"""Episode title scoring — word weighting, phrase matching, position decay."""

from __future__ import annotations

import re
from collections import Counter

from strophalos.types import Episode


def tokenize(text: str) -> list[str]:
    """Split into lowercase alphanumeric words."""
    return re.findall(r"[a-z0-9]+", text.lower())


def compute_word_weights(episodes: list[Episode]) -> dict[str, float]:
    """Compute per-word scoring weights. Common words get reduced weight."""
    n = len(episodes)
    if n == 0:
        return {}

    doc_freq: Counter[str] = Counter()
    for ep in episodes:
        words = set(tokenize(ep.title))
        for w in words:
            doc_freq[w] += 1

    weights: dict[str, float] = {}
    for word, count in doc_freq.items():
        weights[word] = 0.1 if count / n > 0.5 else 1.0
    return weights


def position_weight(t: float, duration: float) -> float:
    """Weight by position in episode. Smooth taper from 1.5x at start to 1.0x by 20%."""
    if duration <= 0:
        return 1.0
    frac = t / duration
    if frac <= 0.20:
        return 1.0 + 0.5 * (1.0 - frac / 0.20)
    if frac >= 0.75:
        return 0.3
    return 1.0


def score_match(
    episode: Episode,
    subtitle_texts: list[tuple[float, str]],
    word_weights: dict[str, float],
    file_duration: float,
) -> float:
    """Score how well subtitle text matches an episode title.

    Each title word is scored at most once, using its best position weight
    across all subtitle cues. Phrase matching (all discriminating words in a
    single cue) provides a strong bonus.
    """
    title_words = tokenize(episode.title)
    if not title_words:
        return 0.0

    # Discriminating words = the ones that actually distinguish episodes
    disc_words = [w for w in title_words if word_weights.get(w, 1.0) > 0.5]

    # Phrase match: if all discriminating words appear in a single subtitle cue,
    # that's likely the title card. This is a very strong signal.
    phrase_bonus = 0.0
    if disc_words:
        for timestamp, text in subtitle_texts:
            cue_words = set(tokenize(text))
            if all(w in cue_words for w in disc_words):
                pw = position_weight(timestamp, file_duration)
                phrase_bonus = max(phrase_bonus, 5.0 * pw)
                break

    # Per-word scoring: each title word scored once at its best position
    best_pw: dict[str, float] = {}
    for timestamp, text in subtitle_texts:
        sub_words = set(tokenize(text))
        pw = position_weight(timestamp, file_duration)
        for word in title_words:
            if word in sub_words:
                best_pw[word] = max(best_pw.get(word, 0.0), pw)

    word_score = 0.0
    for word in title_words:
        if word in best_pw:
            weight = word_weights.get(word, 1.0)
            word_score += weight * best_pw[word]

    return word_score + phrase_bonus
