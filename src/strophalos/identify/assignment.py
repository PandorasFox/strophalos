"""Episode assignment — Hungarian algorithm, window sliding, ordering."""

from __future__ import annotations

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
) -> list[list[float]]:
    """Build the N x M score matrix for files against a window of episodes."""
    matrix: list[list[float]] = []
    for f in files:
        row: list[float] = []
        for ep in window:
            dur_score = 0.0
            if ep.runtime_seconds > 0:
                dur_score = max(0, 1.0 - abs(f.duration_seconds - ep.runtime_seconds) / ep.runtime_seconds) * 2.0
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
) -> list[tuple[int, int, float]]:
    """Assign files to episodes within a window. Returns (file_idx, ep_idx, score)."""
    n = len(files)
    m = len(window_episodes)

    matrix = _build_score_matrix(files, window_episodes, reference_subs)

    # Try forward ordering: bonus for maintaining disc order = episode order
    forward_matrix = [row[:] for row in matrix]
    for i in range(n):
        if i < m:
            forward_matrix[i][i] += 0.5

    # Try reverse ordering
    reverse_matrix = [row[:] for row in matrix]
    for i in range(n):
        rev_j = m - 1 - i
        if 0 <= rev_j < m:
            reverse_matrix[i][rev_j] += 0.5

    # Solve all three, pick best total
    candidates = [
        ("forward", hungarian_assignment(forward_matrix)),
        ("reverse", hungarian_assignment(reverse_matrix)),
        ("unordered", hungarian_assignment(matrix)),
    ]

    best_name = ""
    best_result: list[tuple[int, int, float]] = []
    best_total = -1.0

    for name, assignments in candidates:
        total = sum(matrix[i][j] for i, j, _ in assignments if i < n and j < m)
        if total > best_total:
            best_total = total
            best_result = [(i, j, matrix[i][j]) for i, j, _ in assignments if i < n and j < m]
            best_name = name

    if best_name:
        print(f"  Ordering: {best_name} (score={best_total:.2f})")

    return best_result
