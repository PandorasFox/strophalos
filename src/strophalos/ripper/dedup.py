"""BD title filtering — drop duplicates and bitrate outliers before classification.

Two preprocessing passes run over the raw `titles` dict from `scan_disc`:

1. `dedup_segment_variants` — makemkvcon often exposes a single playable
   chunk twice: once as a `.mpls` playlist (carrying chapter metadata) and
   once as the raw `.m2ts` stream.  Both share the same source segments
   (TINFO attr 26); we keep one per segment set, preferring `.mpls`.

2. `filter_bitrate_outliers` — TV episodes on a single disc are encoded
   at a consistent bitrate; recap reels, making-of featurettes, and promo
   loops typically sit in the duration cluster but at a much lower bitrate.
   We drop titles whose `size/duration` falls far below the median so the
   identify pass isn't fighting a mismatched extra item for a slot.

DVD scans (lsdvd) don't populate attrs 10/26, so both passes are no-ops
for DVDs — no special-casing needed at the call site.
"""

from __future__ import annotations

from strophalos.ripper.cluster import episode_duration_cluster
from strophalos.ripper.scan import get_title_durations, get_title_sizes

# TINFO attribute IDs used here
_ATTR_SOURCE_FN = 16
_ATTR_SEGMENT_MAP = 26


def _segment_key(attrs: dict[int, str]) -> frozenset[int] | None:
    """Parse TINFO attr 26 ('63' or '13,14') into a hashable set of segment ids."""
    raw = attrs.get(_ATTR_SEGMENT_MAP)
    if not raw:
        return None
    try:
        return frozenset(int(s) for s in raw.split(","))
    except ValueError:
        return None


def _is_mpls(attrs: dict[int, str]) -> bool:
    return attrs.get(_ATTR_SOURCE_FN, "").lower().endswith(".mpls")


def dedup_segment_variants(
    titles: dict[int, dict[int, str]],
) -> tuple[dict[int, dict[int, str]], list[tuple[int, int]]]:
    """Collapse BD titles whose source segment sets are identical.

    Keeps one title per unique segment set, preferring the `.mpls`-sourced
    variant (has chapter metadata) and falling back to the lowest title id
    when the variants are the same type.  Titles missing attr 26 pass
    through unchanged (each is its own singleton).

    Returns (filtered_titles, dropped) where `dropped` is a list of
    (dropped_tid, kept_tid) pairs suitable for logging.
    """
    # Group by segment set.  None-key titles (no seg_map) are kept verbatim.
    groups: dict[frozenset[int], list[int]] = {}
    loners: list[int] = []
    for tid in sorted(titles.keys()):
        key = _segment_key(titles[tid])
        if key is None:
            loners.append(tid)
        else:
            groups.setdefault(key, []).append(tid)

    keepers: set[int] = set(loners)
    dropped: list[tuple[int, int]] = []

    for members in groups.values():
        if len(members) == 1:
            keepers.add(members[0])
            continue
        # Prefer .mpls, then lowest title id.
        mpls = [tid for tid in members if _is_mpls(titles[tid])]
        kept = min(mpls) if mpls else min(members)
        keepers.add(kept)
        for tid in members:
            if tid != kept:
                dropped.append((tid, kept))

    filtered = {tid: attrs for tid, attrs in titles.items() if tid in keepers}
    dropped.sort()
    return filtered, dropped


def filter_bitrate_outliers(
    titles: dict[int, dict[int, str]],
    min_ratio: float = 0.5,
    min_titles: int = 3,
) -> tuple[dict[int, dict[int, str]], list[tuple[int, float, float]]]:
    """Drop titles whose encoding bitrate is far below the episode median.

    Episodes on one disc share an encoder pass and come out at similar
    bitrates (variance within ~20%).  Featurettes, "previously on" reels,
    and menu loops are usually encoded at much lower bitrates (often 3-5x
    less), so a single ratio threshold separates them cleanly from the
    episode cluster without needing k-means.

    The duration cluster comes from `episode_duration_cluster` — the same
    rule used by classify, so a featurette that classify picks up as an
    episode is guaranteed to be evaluated here too.  Only cluster members
    can be dropped; short non-cluster titles pass through because
    duration-based classification already handles them.  When the cluster
    has fewer than `min_titles` the filter no-ops — medians off small
    samples are noise.

    Returns (filtered, dropped) where `dropped` is
    `(tid, bitrate_mbps, median_mbps)` tuples for logging.
    """
    sizes = get_title_sizes(titles)
    durations = get_title_durations(titles)

    bitrates: dict[int, float] = {}
    for tid in titles:
        size = sizes.get(tid)
        dur = durations.get(tid)
        if size and dur and dur > 0:
            bitrates[tid] = size / dur  # bytes/sec

    if not durations:
        return titles, []

    cluster_tids_all, _ = episode_duration_cluster(durations)
    # Cluster members must also have computable bitrate.
    cluster_tids = [tid for tid in cluster_tids_all if tid in bitrates]

    if len(cluster_tids) < min_titles:
        return titles, []

    sorted_br = sorted(bitrates[tid] for tid in cluster_tids)
    median = sorted_br[len(sorted_br) // 2]
    threshold = median * min_ratio

    dropped: list[tuple[int, float, float]] = []
    keepers: set[int] = set(titles.keys())
    for tid in cluster_tids:
        br = bitrates[tid]
        if br < threshold:
            keepers.discard(tid)
            dropped.append((tid, br * 8 / 1_000_000, median * 8 / 1_000_000))

    filtered = {tid: attrs for tid, attrs in titles.items() if tid in keepers}
    dropped.sort()
    return filtered, dropped
