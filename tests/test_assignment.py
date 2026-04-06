"""Tests for episode assignment — Hungarian algorithm, window sliding, ordering."""

from __future__ import annotations

from pathlib import Path

from strophalos.identify.assignment import (
    _build_score_matrix,
    assign_episodes,
    find_best_window,
    hungarian_assignment,
)
from strophalos.types import Episode, RippedFile

# ---------------------------------------------------------------------------
# hungarian_assignment
# ---------------------------------------------------------------------------


class TestHungarianAssignment:
    def test_empty(self):
        assert hungarian_assignment([]) == []

    def test_identity(self):
        """Diagonal matrix: optimal is (0,0), (1,1), (2,2)."""
        matrix = [
            [10.0, 0.0, 0.0],
            [0.0, 10.0, 0.0],
            [0.0, 0.0, 10.0],
        ]
        result = hungarian_assignment(matrix)
        assigned = {(r, c) for r, c, _ in result}
        assert (0, 0) in assigned
        assert (1, 1) in assigned
        assert (2, 2) in assigned
        total = sum(s for _, _, s in result)
        assert total == 30.0

    def test_off_diagonal_optimal(self):
        """When optimal assignment is NOT the diagonal."""
        matrix = [
            [1.0, 10.0],
            [10.0, 1.0],
        ]
        result = hungarian_assignment(matrix)
        assigned = {(r, c) for r, c, _ in result}
        assert (0, 1) in assigned
        assert (1, 0) in assigned
        total = sum(s for _, _, s in result)
        assert total == 20.0

    def test_rectangular_more_cols(self):
        """More columns than rows: each row gets assigned to the best column."""
        matrix = [
            [1.0, 5.0, 3.0],
            [4.0, 2.0, 6.0],
        ]
        result = hungarian_assignment(matrix)
        assert len(result) >= 2
        total = sum(s for _, _, s in result)
        # Optimal: row 0 → col 1 (5), row 1 → col 2 (6) = 11
        assert total == 11.0

    def test_rectangular_more_rows(self):
        """More rows than columns: some rows go unassigned (padded)."""
        matrix = [
            [3.0, 1.0],
            [1.0, 5.0],
            [4.0, 2.0],
        ]
        result = hungarian_assignment(matrix)
        # Should assign all 3 rows to 2 cols, padding creates a virtual 3rd col
        assigned_rows = {r for r, _, _ in result}
        assert len(assigned_rows) >= 2

    def test_single_element(self):
        matrix = [[7.0]]
        result = hungarian_assignment(matrix)
        assert result == [(0, 0, 7.0)]

    def test_all_equal(self):
        """When all scores are equal, any assignment works. Total should be N * value."""
        matrix = [
            [5.0, 5.0, 5.0],
            [5.0, 5.0, 5.0],
            [5.0, 5.0, 5.0],
        ]
        result = hungarian_assignment(matrix)
        total = sum(s for _, _, s in result)
        assert total == 15.0


# ---------------------------------------------------------------------------
# _build_score_matrix
# ---------------------------------------------------------------------------


class TestBuildScoreMatrix:
    def test_duration_scoring(self):
        """Files with matching durations should score higher for matching episodes."""
        files = [
            RippedFile(path=Path("a.mkv"), duration_seconds=1400),
            RippedFile(path=Path("b.mkv"), duration_seconds=2800),
        ]
        episodes = [
            Episode(1, 1, "Short", runtime_seconds=1400),
            Episode(1, 2, "Long", runtime_seconds=2800),
        ]
        matrix = _build_score_matrix(files, episodes, {})
        # file 0 (1400s) should score higher against ep 0 (1400s) than ep 1 (2800s)
        assert matrix[0][0] > matrix[0][1]
        # file 1 (2800s) should score higher against ep 1 (2800s) than ep 0 (1400s)
        assert matrix[1][1] > matrix[1][0]

    def test_no_runtime_info(self):
        """Episodes with runtime_seconds=0 should get dur_score=0, not crash."""
        files = [RippedFile(path=Path("a.mkv"), duration_seconds=1400)]
        episodes = [Episode(1, 1, "Unknown Runtime", runtime_seconds=0)]
        matrix = _build_score_matrix(files, episodes, {})
        assert matrix[0][0] == 0.0  # no duration score, no subtitle score

    def test_perfect_duration_match(self):
        """Exact duration match should get max duration score of 2.0."""
        files = [RippedFile(path=Path("a.mkv"), duration_seconds=1400)]
        episodes = [Episode(1, 1, "", runtime_seconds=1400)]
        matrix = _build_score_matrix(files, episodes, {})
        assert matrix[0][0] == 2.0


# ---------------------------------------------------------------------------
# find_best_window
# ---------------------------------------------------------------------------


class TestFindBestWindow:
    def test_exact_match_by_duration(self):
        """Window should land where durations align best."""
        # 3 files with durations that match episodes 5-7
        files = [
            RippedFile(path=Path("a.mkv"), duration_seconds=1400),
            RippedFile(path=Path("b.mkv"), duration_seconds=1500),
            RippedFile(path=Path("c.mkv"), duration_seconds=1350),
        ]
        # 10 episodes, only eps 5-7 have matching runtimes
        episodes = []
        for i in range(10):
            if i == 4:
                rt = 1400
            elif i == 5:
                rt = 1500
            elif i == 6:
                rt = 1350
            else:
                rt = 2700  # very different
            episodes.append(Episode(1, i + 1, f"Episode {i + 1}", runtime_seconds=rt))

        start, score = find_best_window(files, episodes, {})
        assert start == 4  # 0-indexed: episodes[4:7]
        assert score > 0

    def test_more_files_than_episodes(self):
        files = [RippedFile(path=Path(f"{i}.mkv"), duration_seconds=1400) for i in range(5)]
        episodes = [Episode(1, i + 1, f"Ep {i + 1}", runtime_seconds=1400) for i in range(3)]
        start, score = find_best_window(files, episodes, {})
        assert start == 0
        assert score == 0.0  # n > m returns 0

    def test_empty(self):
        assert find_best_window([], [], {}) == (0, 0.0)


# ---------------------------------------------------------------------------
# assign_episodes
# ---------------------------------------------------------------------------


class TestAssignEpisodes:
    def test_forward_order_preferred(self):
        """When scores are symmetric, forward ordering should get the tiebreak bonus."""
        files = [
            RippedFile(path=Path("a.mkv"), duration_seconds=1400),
            RippedFile(path=Path("b.mkv"), duration_seconds=1400),
        ]
        episodes = [
            Episode(1, 1, "", runtime_seconds=1400),
            Episode(1, 2, "", runtime_seconds=1400),
        ]
        result = assign_episodes(files, episodes, {})
        assignments = {fi: ei for fi, ei, _ in result}
        # Forward order: file 0 → ep 0, file 1 → ep 1
        assert assignments[0] == 0
        assert assignments[1] == 1

    def test_strong_signal_overrides_order(self):
        """Strong duration mismatch should override the order bonus."""
        files = [
            RippedFile(path=Path("a.mkv"), duration_seconds=2800),  # long
            RippedFile(path=Path("b.mkv"), duration_seconds=1400),  # short
        ]
        episodes = [
            Episode(1, 1, "", runtime_seconds=1400),  # short
            Episode(1, 2, "", runtime_seconds=2800),  # long
        ]
        result = assign_episodes(files, episodes, {})
        assignments = {fi: ei for fi, ei, _ in result}
        # Duration signal should override forward bonus: file 0 (2800) → ep 1 (2800)
        assert assignments[0] == 1
        assert assignments[1] == 0
