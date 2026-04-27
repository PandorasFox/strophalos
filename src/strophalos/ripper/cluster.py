"""Episode duration clustering — the shared rule for identifying the
"episode cluster" on a disc.

Both `classify._score_tv` (picks titles to rip) and
`dedup.filter_bitrate_outliers` (filters featurette-bitrate imposters)
need to agree on which titles look like episodes by duration, so the two
filters don't drift out of sync.  Split into this module so changing the
floor or band is a one-point edit.
"""

from __future__ import annotations

EPISODE_DURATION_FLOOR = 600
EPISODE_BAND_LOW = 0.5
EPISODE_BAND_HIGH = 2.0


def episode_duration_cluster(
    durations: dict[int, int],
    floor_sec: int = EPISODE_DURATION_FLOOR,
    band_low: float = EPISODE_BAND_LOW,
    band_high: float = EPISODE_BAND_HIGH,
) -> tuple[list[int], int]:
    """Identify titles that form the episode cluster by duration.

    Three-step rule:
      1. Candidates: titles with `dur >= floor_sec`.
      2. Median over candidate durations.
      3. Cluster: candidates whose dur ∈ [band_low, band_high] × median.

    Median (not max) anchors the band so a single double-length finale or
    a play-all title doesn't push episode-length outliers (e.g. BSG S2D5
    t7: 28min featurette alongside a 68min 2-part finale) out of the
    cluster.

    Returns (cluster_tids_sorted, median_dur).  ([], 0) when no candidates.
    """
    candidates = [tid for tid, dur in durations.items() if dur >= floor_sec]
    if not candidates:
        return [], 0

    cand_durs = sorted(durations[tid] for tid in candidates)
    median_dur = cand_durs[len(cand_durs) // 2]

    low = median_dur * band_low
    high = median_dur * band_high
    cluster = sorted(tid for tid in candidates if low <= durations[tid] <= high)
    return cluster, median_dur
