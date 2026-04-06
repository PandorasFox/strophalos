"""Episode scoring — time-aligned subtitle similarity comparison."""

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


def score_subtitle_similarity(
    extracted_cues: list[tuple[float, str]],
    reference_cues: list[tuple[float, str]],
) -> float:
    """Score similarity between extracted and reference subtitles.

    Uses time-aligned word-level Jaccard similarity with offset compensation.
    Tries timing offsets (0, ±5s, ±10s) and returns the best score.

    Returns a score where higher = better match. Scaled so a perfect
    match on clean text yields ~10.0.
    """
    if not extracted_cues or not reference_cues:
        return 0.0

    bucket_width = 30.0
    ref_buckets = _bucket_cues(reference_cues, bucket_width)

    best_score = 0.0
    for offset in (0.0, 5.0, -5.0, 10.0, -10.0):
        ext_buckets = _bucket_cues(extracted_cues, bucket_width, offset=offset)
        score = _jaccard_score(ext_buckets, ref_buckets)
        if score > best_score:
            best_score = score

    return best_score
