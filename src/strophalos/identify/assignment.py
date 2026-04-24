"""Episode assignment — Hungarian algorithm, window sliding, ordering."""

from __future__ import annotations

from statistics import median

from strophalos.identify.scoring import score_subtitle_similarity
from strophalos.types import Episode, RippedFile


def hungarian_assignment(score_matrix: list[list[float]]) -> list[tuple[int, int, float]]:
    """Optimal assignment maximizing total score. Returns (row, col, score) triples.

    Pure Python implementation of the Hungarian algorithm for rectangular matrices.
    Pads to square with zeros, then uses the Munkres method.
    """
    n_rows = len(score_matrix)
    if n_rows == 0:
        return []
    n_cols = len(score_matrix[0])
    n = max(n_rows, n_cols)

    # Negate for minimization and pad to square
    max_val = max(max(row) for row in score_matrix) if score_matrix else 0
    cost: list[list[float]] = []
    for i in range(n):
        row: list[float] = []
        for j in range(n):
            if i < n_rows and j < n_cols:
                row.append(max_val - score_matrix[i][j])
            else:
                row.append(0.0)
        cost.append(row)

    # Munkres algorithm
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (n + 1)
        used = [False] * (n + 1)

        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = -1

            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j

            if j1 == -1:
                break

            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta

            j0 = j1
            if p[j0] == 0:
                break

        while j0 != 0:
            p[j0] = p[way[j0]]
            j0 = way[j0]

    results: list[tuple[int, int, float]] = []
    for j in range(1, n + 1):
        i = p[j] - 1
        if i < n_rows and j - 1 < n_cols:
            results.append((i, j - 1, score_matrix[i][j - 1]))

    return results


def _build_score_matrix(
    files: list[RippedFile],
    window: list[Episode],
    reference_subs: dict[tuple[int, int], list[tuple[float, str]]],
    double_indices: frozenset[int] = frozenset(),
) -> list[list[float]]:
    """Build the N x M score matrix for files against a window of episodes.

    *double_indices*: file indices whose duration should be compared against
    2× the episode runtime (double-length episodes covering two slots).
    """
    matrix: list[list[float]] = []
    for fi, f in enumerate(files):
        row: list[float] = []
        is_double = fi in double_indices
        for ep in window:
            dur_score = 0.0
            if ep.runtime_seconds > 0:
                expected = ep.runtime_seconds * (2 if is_double else 1)
                dur_score = max(0, 1.0 - abs(f.duration_seconds - expected) / expected) * 2.0
            ref = reference_subs.get((ep.season, ep.episode), [])
            sub_score = score_subtitle_similarity(f.subtitle_texts, ref) if ref and f.subtitle_texts else 0.0
            row.append(dur_score + sub_score)
        matrix.append(row)
    return matrix


def find_best_window(
    files: list[RippedFile],
    episodes: list[Episode],
    reference_subs: dict[tuple[int, int], list[tuple[float, str]]],
) -> tuple[int, float]:
    """Slide a window of len(files) across episodes, return (start_idx, best_score).

    For each window position, compute the optimal assignment score (Hungarian)
    so that a few noisy matches don't drag the window to the wrong position.
    """
    n = len(files)
    m = len(episodes)
    if n == 0 or m == 0 or n > m:
        return 0, 0.0

    best_start = 0
    best_score = -1.0

    for start in range(m - n + 1):
        window = episodes[start : start + n]
        matrix = _build_score_matrix(files, window, reference_subs)
        assignments = hungarian_assignment(matrix)
        total = sum(matrix[i][j] for i, j, _ in assignments if i < n and j < n)

        if total > best_score:
            best_score = total
            best_start = start

    return best_start, best_score


def assign_episodes(
    files: list[RippedFile],
    window_episodes: list[Episode],
    reference_subs: dict[tuple[int, int], list[tuple[float, str]]],
    double_indices: frozenset[int] = frozenset(),
) -> list[tuple[int, int, float]]:
    """Assign files to episodes within a window. Returns (file_idx, ep_idx, score).

    Pure Hungarian over the raw score matrix — no order bonuses. Discs can be
    in any order; trust only the evidence in the score matrix. Weak matches are
    returned with their real score so the caller can gate on it.
    """
    n = len(files)
    m = len(window_episodes)
    matrix = _build_score_matrix(files, window_episodes, reference_subs, double_indices)
    assignments = hungarian_assignment(matrix)
    return [(i, j, matrix[i][j]) for i, j, _ in assignments if i < n and j < m]


def build_double_episode_window(
    files: list[RippedFile],
    episodes: list[Episode],
) -> tuple[list[Episode], frozenset[int], dict[int, Episode]] | None:
    """Build a window that skips episodes absorbed by double-length files.

    Detects files whose duration is ≥1.5× the median episode runtime
    (i.e. a double-feature covering two episode slots).  For each such
    file, the episode after its match is skipped in the window so the
    remaining files align to the correct later episodes.

    Returns ``(window, double_indices, skipped_map)`` — the adjusted
    episode window, the set of file indices that are double-length, and
    a mapping from file index to the episode absorbed by that double —
    or *None* when no double-length files are detected or there aren't
    enough episodes.
    """
    runtimes = [ep.runtime_seconds for ep in episodes if ep.runtime_seconds > 0]
    if not runtimes:
        return None

    med = median(runtimes)
    if med <= 0:
        return None

    double_flags = [f.duration_seconds >= 1.5 * med for f in files]
    if not any(double_flags):
        return None
    # If every file is flagged, the episode runtime data is probably wrong
    # (e.g. TMDb reports half the real length).  A genuine double-episode
    # disc always has at least one normal-length file alongside the double.
    if all(double_flags):
        return None

    window: list[Episode] = []
    skipped_map: dict[int, Episode] = {}
    ep_cursor = 0
    for file_idx, is_double in enumerate(double_flags):
        if ep_cursor >= len(episodes):
            break
        window.append(episodes[ep_cursor])
        if is_double and ep_cursor + 1 < len(episodes):
            skipped_map[file_idx] = episodes[ep_cursor + 1]
        ep_cursor += 2 if is_double else 1

    if len(window) != len(files):
        return None

    double_indices = frozenset(i for i, d in enumerate(double_flags) if d)
    return window, double_indices, skipped_map
