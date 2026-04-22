"""Tests for episode assignment — Hungarian algorithm, window sliding, ordering."""

from __future__ import annotations

from pathlib import Path

from strophalos.identify.assignment import (
    _build_score_matrix,
    assign_episodes,
    build_double_episode_window,
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


# ---------------------------------------------------------------------------
# build_double_episode_window
# ---------------------------------------------------------------------------


class TestBuildDoubleEpisodeWindow:
    def _make_episodes(self, count: int, runtime: float = 2700, season: int = 2) -> list[Episode]:
        return [Episode(season, i + 1, f"Episode {i + 1}", runtime_seconds=runtime) for i in range(count)]

    def test_no_doubles_returns_none(self):
        """All files are normal length — no skip window needed."""
        files = [RippedFile(path=Path(f"{i}.mkv"), duration_seconds=2700) for i in range(3)]
        episodes = self._make_episodes(6)
        assert build_double_episode_window(files, episodes) is None

    def test_single_double_at_start(self):
        """First file is double-length — should skip E02, return [E01, E03, E04]."""
        files = [
            RippedFile(path=Path("a.mkv"), duration_seconds=5400),  # ~2x normal
            RippedFile(path=Path("b.mkv"), duration_seconds=2700),
            RippedFile(path=Path("c.mkv"), duration_seconds=2700),
        ]
        episodes = self._make_episodes(6)
        result = build_double_episode_window(files, episodes)
        assert result is not None
        window, double_indices, skipped_map = result
        assert [ep.episode for ep in window] == [1, 3, 4]
        assert double_indices == frozenset({0})
        assert 0 in skipped_map
        assert skipped_map[0].episode == 2  # E02 absorbed by file 0

    def test_mr_robot_s02_scenario(self):
        """Mr. Robot S02: file 0 is ~90 min (E01+E02), files 1-2 are ~45 min.

        TMDb reports E01-E04 all at ~45 min each.
        The double-skip window should produce [E01, E03, E04].
        """
        files = [
            RippedFile(path=Path("t00.mkv"), duration_seconds=5400),  # 90 min
            RippedFile(path=Path("t01.mkv"), duration_seconds=2700),  # 45 min
            RippedFile(path=Path("t02.mkv"), duration_seconds=2700),  # 45 min
        ]
        episodes = [
            Episode(2, 1, "eps2.0_unm4sk-pt1.tc", runtime_seconds=2700),
            Episode(2, 2, "eps2.0_unm4sk-pt2.tc", runtime_seconds=2700),
            Episode(2, 3, "eps2.0_logic-b0mb.hc", runtime_seconds=2700),
            Episode(2, 4, "eps2.0_init_1.asec", runtime_seconds=2700),
        ]
        result = build_double_episode_window(files, episodes)
        assert result is not None
        window, double_indices, skipped_map = result
        codes = [ep.code for ep in window]
        assert codes == ["S02E01", "S02E03", "S02E04"]
        assert double_indices == frozenset({0})
        assert skipped_map[0].code == "S02E02"

    def test_mr_robot_s02_scoring(self):
        """Double-episode window should outscore sequential when file 0 is 2x length.

        With duration-aware scoring, the skip window should give t00 a much
        better duration score (compared to 2× episode runtime) than the
        sequential window (compared to 1× runtime).
        """
        files = [
            RippedFile(path=Path("t00.mkv"), duration_seconds=4938),  # actual: 82 min
            RippedFile(path=Path("t01.mkv"), duration_seconds=3782),  # actual: 63 min
            RippedFile(path=Path("t02.mkv"), duration_seconds=3922),  # actual: 65 min
        ]
        episodes = [
            Episode(2, 1, "E1", runtime_seconds=2700),
            Episode(2, 2, "E2", runtime_seconds=2700),
            Episode(2, 3, "E3", runtime_seconds=2700),
            Episode(2, 4, "E4", runtime_seconds=2700),
        ]
        result = build_double_episode_window(files, episodes)
        assert result is not None
        skip_window, double_indices, _ = result

        # Sequential window scores t00 against 1× runtime → poor duration fit
        seq_matrix = _build_score_matrix(files, episodes[:3], {})
        # Skip window scores t00 against 2× runtime → much better fit
        skip_matrix = _build_score_matrix(files, skip_window, {}, double_indices)

        # t00 vs E01: skip window should give dramatically better duration score
        assert skip_matrix[0][0] > seq_matrix[0][0] + 1.0

    def test_double_in_middle(self):
        """Double-length file in position 1 — skip over the absorbed episode."""
        files = [
            RippedFile(path=Path("a.mkv"), duration_seconds=2700),
            RippedFile(path=Path("b.mkv"), duration_seconds=5400),  # double
            RippedFile(path=Path("c.mkv"), duration_seconds=2700),
        ]
        episodes = self._make_episodes(6)
        result = build_double_episode_window(files, episodes)
        assert result is not None
        window, double_indices, skipped_map = result
        assert [ep.episode for ep in window] == [1, 2, 4]
        assert double_indices == frozenset({1})
        assert skipped_map[1].episode == 3  # E03 absorbed by file 1

    def test_not_enough_episodes_returns_none(self):
        """If skipping leaves too few episodes, return None."""
        files = [
            RippedFile(path=Path("a.mkv"), duration_seconds=5400),
            RippedFile(path=Path("b.mkv"), duration_seconds=5400),
        ]
        # Only 2 episodes: file 0 → ep 1, skip ep 2, file 1 → nothing left
        episodes = self._make_episodes(2)
        assert build_double_episode_window(files, episodes) is None

    def test_no_runtime_info_returns_none(self):
        """Episodes with no runtime data — can't detect doubles."""
        files = [RippedFile(path=Path("a.mkv"), duration_seconds=5400)]
        episodes = [Episode(1, 1, "Ep 1", runtime_seconds=0)]
        assert build_double_episode_window(files, episodes) is None
