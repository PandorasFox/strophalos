"""Episode scoring — subtitle similarity comparison (time-aligned and bag-of-words)."""

from __future__ import annotations

import re
from collections import defaultdict


def tokenize(text: str) -> list[str]:
    """Split into lowercase alphanumeric words."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _normalize_cue(text: str) -> str:
    """Strip formatting tags, lowercase, collapse whitespace."""
    text = re.sub(r"<[^>]+>", "", text)  # HTML tags
    text = re.sub(r"\{[^}]*\}", "", text)  # ASS/SSA tags
    text = text.lower().strip()
    return re.sub(r"\s+", " ", text)


def _bucket_cues(
    cues: list[tuple[float, str]],
    bucket_width: float,
    offset: float = 0.0,
) -> dict[int, set[str]]:
    """Group cue words into time buckets.

    Returns {bucket_index: set_of_words}. The offset shifts cue timestamps
    before bucketing (for compensating intro timing differences).
    """
    buckets: dict[int, set[str]] = defaultdict(set)
    for timestamp, text in cues:
        adjusted = timestamp + offset
        if adjusted < 0:
            continue
        bucket = int(adjusted // bucket_width)
        words = tokenize(_normalize_cue(text))
        buckets[bucket].update(words)
    return buckets


def _jaccard_score(
    extracted_buckets: dict[int, set[str]],
    reference_buckets: dict[int, set[str]],
) -> float:
    """Compute weighted Jaccard similarity across shared time buckets.

    For each bucket present in both extracted and reference, computes
    |intersection| / |union| weighted by the number of reference words.
    Returns the weighted sum divided by total reference words, scaled by 10.
    """
    total_ref_words = 0
    weighted_sum = 0.0

    for bucket_idx, ref_words in reference_buckets.items():
        if not ref_words:
            continue
        n_ref = len(ref_words)
        total_ref_words += n_ref

        ext_words = extracted_buckets.get(bucket_idx)
        if not ext_words:
            continue

        intersection = len(ref_words & ext_words)
        union = len(ref_words | ext_words)
        if union > 0:
            weighted_sum += (intersection / union) * n_ref

    if total_ref_words == 0:
        return 0.0

    return (weighted_sum / total_ref_words) * 10.0


def _bag_of_words_score(
    extracted_cues: list[tuple[float, str]],
    reference_cues: list[tuple[float, str]],
) -> float:
    """Time-independent Jaccard over all words in both cue lists.

    Ignores timestamps entirely — just compares the vocabulary of the two
    subtitle streams.  Robust to arbitrary timing offsets (DVD vs broadcast,
    whisper-generated timestamps vs hand-synced SRT).
    """
    ext_words: set[str] = set()
    for _, text in extracted_cues:
        ext_words.update(tokenize(_normalize_cue(text)))
    ref_words: set[str] = set()
    for _, text in reference_cues:
        ref_words.update(tokenize(_normalize_cue(text)))

    if not ext_words or not ref_words:
        return 0.0

    intersection = len(ext_words & ref_words)
    union = len(ext_words | ref_words)
    return (intersection / union) * 10.0 if union else 0.0


def score_subtitle_similarity(
    extracted_cues: list[tuple[float, str]],
    reference_cues: list[tuple[float, str]],
) -> float:
    """Score similarity between extracted and reference subtitles.

    Tries two strategies and returns the better score:
      1. Time-aligned Jaccard (30s buckets, ±10s offset compensation)
      2. Bag-of-words Jaccard (ignores timing entirely)

    Strategy 1 is more precise when timestamps are close (embedded SRT vs
    reference SRT).  Strategy 2 handles arbitrary timing mismatches — DVD
    rips, whisper transcriptions, etc. — where the word overlap is the
    real signal.

    Returns a score where higher = better match. Scaled so a perfect
    match on clean text yields ~10.0.
    """
    if not extracted_cues or not reference_cues:
        return 0.0

    # Time-aligned scoring
    bucket_width = 30.0
    ref_buckets = _bucket_cues(reference_cues, bucket_width)

    best_score = 0.0
    for offset in (0.0, 5.0, -5.0, 10.0, -10.0):
        ext_buckets = _bucket_cues(extracted_cues, bucket_width, offset=offset)
        score = _jaccard_score(ext_buckets, ref_buckets)
        if score > best_score:
            best_score = score

    # Bag-of-words fallback — dominates when timing is misaligned
    bow = _bag_of_words_score(extracted_cues, reference_cues)
    if bow > best_score:
        best_score = bow

    return best_score
