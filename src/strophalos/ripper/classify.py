"""Disc classification — score disc as movie, TV, music, or unknown."""

from __future__ import annotations

from strophalos.backends.musicbrainz import score_musicbrainz
from strophalos.backends.tmdb import score_title_search


def _score_movie(
    sorted_titles: list[tuple[int, int]],
    longest_tid: int,
    longest_dur: int,
    rest: list[tuple[int, int]],
    meaningful: list[tuple[int, int]],
) -> tuple[float, list[int], str]:
    """Score how well the disc fits a movie pattern. Returns (score, titles, reason)."""
    score = 0.0
    reasons: list[str] = []

    # Duration dominance: main feature much longer than anything else
    if rest:
        second_dur = rest[0][1]
        if second_dur > 0:
            ratio = longest_dur / second_dur
            if ratio >= 4:
                score += 0.3
                reasons.append(f"dominant ({ratio:.1f}x)")
            elif ratio >= 2:
                score += 0.15
                reasons.append(f"longer ({ratio:.1f}x)")

    # Feature length (>= 1 hour)
    if longest_dur >= 3600:
        score += 0.2
        reasons.append("feature-length")

    # Extras pattern: everything else is much shorter than main
    if rest:
        extras = [(tid, dur) for tid, dur in rest if dur < longest_dur * 0.5]
        if len(extras) == len(rest):
            score += 0.2
            reasons.append("short extras")

    # Few non-trivial extras — movies typically have 0-3, not 9
    non_trivial = [(tid, dur) for tid, dur in rest if dur >= 600]
    if len(non_trivial) <= 3:
        score += 0.2
        reasons.append(f"{len(non_trivial)} non-trivial extra(s)")
    elif len(non_trivial) <= 6:
        score += 0.1

    return score, [longest_tid], "; ".join(reasons)


def _score_tv(
    sorted_titles: list[tuple[int, int]],
    longest_tid: int,
    longest_dur: int,
    rest: list[tuple[int, int]],
    meaningful: list[tuple[int, int]],
) -> tuple[float, list[int], str]:
    """Score how well the disc fits a TV pattern. Returns (score, titles, reason)."""
    score = 0.0
    reasons: list[str] = []

    rest_meaningful = [(tid, dur) for tid, dur in meaningful if tid != longest_tid]
    episode_candidates = [(tid, dur) for tid, dur in rest_meaningful if dur >= 600]

    if len(episode_candidates) < 2:
        return 0.0, [], "too few episode candidates"

    ep_durs = [dur for _, dur in episode_candidates]
    median_dur = sorted(ep_durs)[len(ep_durs) // 2]

    in_cluster = [(tid, dur) for tid, dur in episode_candidates if median_dur * 0.5 <= dur <= median_dur * 2.0]

    if len(in_cluster) < 2:
        return 0.0, [], "no duration cluster"

    # Episode uniformity — low coefficient of variation means consistent lengths
    cluster_durs = [dur for _, dur in in_cluster]
    mean_dur = sum(cluster_durs) / len(cluster_durs)
    variance = sum((d - mean_dur) ** 2 for d in cluster_durs) / len(cluster_durs)
    cv = (variance**0.5) / mean_dur if mean_dur > 0 else 1.0

    if cv < 0.05:
        score += 0.35
        reasons.append(f"very uniform (CV {cv:.3f})")
    elif cv < 0.15:
        score += 0.25
        reasons.append(f"uniform (CV {cv:.2f})")
    elif cv < 0.30:
        score += 0.15
        reasons.append(f"somewhat uniform (CV {cv:.2f})")

    # Episode count — more similar titles = stronger TV signal
    if len(in_cluster) >= 6:
        score += 0.3
        reasons.append(f"{len(in_cluster)} episodes")
    elif len(in_cluster) >= 3:
        score += 0.2
        reasons.append(f"{len(in_cluster)} episodes")
    else:
        score += 0.05
        reasons.append(f"{len(in_cluster)} episodes")

    # Play-all detection — longest ≈ sum of cluster
    cluster_sum = sum(dur for _, dur in in_cluster)
    if cluster_sum > 0:
        play_all_diff = abs(longest_dur - cluster_sum) / cluster_sum
        if play_all_diff < 0.10:
            score += 0.3
            reasons.append(f"play-all (title {longest_tid}, {play_all_diff:.1%} off)")

    # Typical episode duration (20–65 min)
    if 1200 <= median_dur <= 3900:
        score += 0.1
        reasons.append("typical episode length")

    # Select episode cluster; include longest only if it's episode-length itself
    titles_to_rip = [tid for tid, _ in in_cluster]
    if median_dur * 0.5 <= longest_dur <= median_dur * 2.0:
        if longest_tid not in titles_to_rip:
            titles_to_rip.append(longest_tid)

    return score, sorted(titles_to_rip), "; ".join(reasons)


def classify_disc(
    durations: dict[int, int],
    chapters: dict[int, int],
    disc_label: str | None = None,
) -> tuple[str, list[int], str, dict | None]:
    """Classify disc as 'tv', 'movie', 'music', or 'unknown' and return titles to rip.

    Returns (disc_type, titles_to_rip, reason, metadata).
    metadata is non-None for music discs (contains MB release info).
    """
    if not durations:
        return "unknown", [], "no titles found", None

    sorted_titles = sorted(durations.items(), key=lambda x: x[1], reverse=True)
    longest_tid, longest_dur = sorted_titles[0]
    rest = sorted_titles[1:]

    meaningful = [(tid, dur) for tid, dur in sorted_titles if dur >= 120]

    # TMDb runtime match is a stronger signal than MB's title-only BD match —
    # suppress the latter when it fires, so films whose soundtracks exist as
    # BD releases (e.g. Princess Mononoke) don't get classified as audio.
    m_boost, t_boost = score_title_search(disc_label, durations)
    tmdb_runtime_match = m_boost >= 0.6

    mb_score, mb_release = score_musicbrainz(disc_label, durations, allow_title_only_match=not tmdb_runtime_match)
    if mb_score > 0.7:
        # Identify the play-all title: longest title with chapter count
        # closest to MB track count
        play_all_tid = longest_tid
        if mb_release and "track_count" in mb_release:
            mb_track_count = mb_release["track_count"]
            best_diff = abs(chapters.get(longest_tid, 0) - mb_track_count)
            for tid, _dur in sorted_titles:
                ch = chapters.get(tid, 0)
                diff = abs(ch - mb_track_count)
                if diff < best_diff:
                    best_diff = diff
                    play_all_tid = tid

        return (
            "music",
            [play_all_tid],
            f"musicbrainz match: {mb_release['artist']} - {mb_release['title']} "
            f"(score={mb_score:.2f}, {mb_release['track_count']} tracks, "
            f"play-all=title {play_all_tid})",
            mb_release,
        )

    if len(meaningful) == 0:
        return "unknown", [t[0] for t in sorted_titles], "no meaningful titles", None

    if len(meaningful) == 1:
        return "movie", [meaningful[0][0]], "single main title", None

    # Score both patterns
    m_score, m_titles, m_reason = _score_movie(sorted_titles, longest_tid, longest_dur, rest, meaningful)
    t_score, t_titles, t_reason = _score_tv(sorted_titles, longest_tid, longest_dur, rest, meaningful)

    m_score += m_boost
    t_score += t_boost

    # Weak MB match logging
    if mb_score > 0.3:
        print(f"  MusicBrainz: weak match (score={mb_score:.2f}), not overriding")

    search_note = ""
    if m_boost or t_boost:
        search_note = f", search: +{m_boost:.1f}m/+{t_boost:.1f}t"

    print(f"\n  Scores: movie={m_score:.2f} [{m_reason}]")
    print(f"          tv={t_score:.2f} [{t_reason}]")

    if t_score > m_score:
        return "tv", t_titles, f"tv={t_score:.2f} > movie={m_score:.2f}{search_note}", None
    elif m_score > t_score:
        return "movie", m_titles, f"movie={m_score:.2f} > tv={t_score:.2f}{search_note}", None
    else:
        return (
            "unknown",
            [tid for tid, _ in meaningful],
            f"tied {m_score:.2f}{search_note}, ripping all {len(meaningful)}",
            None,
        )
