"""Tests for audio Blu-ray disc handling — classification, play-all selection, subset pruning."""

from __future__ import annotations

from unittest.mock import patch

from strophalos.ripper.classify import classify_disc

# ---------------------------------------------------------------------------
# Test data: THE FAR EDGE OF FATE: FINAL FANTASY XIV Original Soundtrack
# MB release: 3c246503-c77c-4d95-b5d1-2309f159315b (52 tracks, 12871s)
# ---------------------------------------------------------------------------

FFXIV_DURATIONS = {
    0: 2030,   # 9 chapters — section (subset)
    1: 2645,   # 9 chapters — section (subset)
    2: 3536,   # 14 chapters — section (subset)
    3: 5818,   # 21 chapters — section (subset)
    4: 2018,   # 9 chapters — section (subset)
    5: 1520,   # 6 chapters — section (subset)
    6: 12894,  # 50 chapters — play-all
    7: 262,    # 0 chapters — bonus tracks
}

FFXIV_CHAPTERS = {
    0: 9, 1: 9, 2: 14, 3: 21, 4: 9, 5: 6, 6: 50, 7: 0,
}

FFXIV_MB_RELEASE = {
    "artist": "祖堅正慶",
    "title": "THE FAR EDGE OF FATE: FINAL FANTASY XIV Original Soundtrack",
    "id": "3c246503-c77c-4d95-b5d1-2309f159315b",
    "track_count": 52,
    "match_method": "play-all",
    "duration_diff_pct": "0.2%",
}


class TestMusicBDClassification:
    """Test that audio BDs are classified as music when MB matches."""

    @patch("strophalos.ripper.classify.score_musicbrainz")
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_ffxiv_classified_as_music(self, mock_tmdb, mock_mb):
        mock_mb.return_value = (0.91, FFXIV_MB_RELEASE)
        disc_type, titles, reason, meta = classify_disc(
            FFXIV_DURATIONS, FFXIV_CHAPTERS,
            "THE FAR EDGE OF FATE： FINAL FANTASY XIV Original Soundtrack",
        )
        assert disc_type == "music"
        assert meta is not None
        assert meta["track_count"] == 52

    @patch("strophalos.ripper.classify.score_musicbrainz")
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_play_all_title_selected(self, mock_tmdb, mock_mb):
        """The play-all title (most chapters, closest to MB track count) should be selected."""
        mock_mb.return_value = (0.91, FFXIV_MB_RELEASE)
        _disc_type, titles, _reason, _meta = classify_disc(
            FFXIV_DURATIONS, FFXIV_CHAPTERS,
            "THE FAR EDGE OF FATE： FINAL FANTASY XIV Original Soundtrack",
        )
        # Title 6 has 50 chapters, closest to 52 MB tracks
        assert titles == [6]

    @patch("strophalos.ripper.classify.score_musicbrainz")
    @patch("strophalos.ripper.classify.score_title_search", return_value=(0.0, 0.0))
    def test_without_mb_match_not_music(self, mock_tmdb, mock_mb):
        """Without a strong MB match, this disc should NOT classify as music."""
        mock_mb.return_value = (0.0, None)
        disc_type, _titles, _reason, _meta = classify_disc(
            FFXIV_DURATIONS, FFXIV_CHAPTERS,
            "THE FAR EDGE OF FATE： FINAL FANTASY XIV Original Soundtrack",
        )
        assert disc_type != "music"


class TestSubsetPruning:
    """Test that subset titles are pruned and bonus titles are kept."""

    def _get_remaining_tids(
        self,
        durations: dict[int, int],
        chapters: dict[int, int],
        play_all_tid: int,
    ) -> list[int]:
        """Replicate the subset pruning logic from rip_video.py."""
        play_all_ch = chapters.get(play_all_tid, 0)
        play_all_dur = durations.get(play_all_tid, 0)

        remaining = []
        for tid, dur in sorted(durations.items()):
            if tid == play_all_tid:
                continue
            ch = chapters.get(tid, 0)
            if ch > 0 and dur < play_all_dur and ch < play_all_ch:
                continue
            remaining.append(tid)
        return remaining

    def test_ffxiv_sections_pruned(self):
        """Titles 0-5 (sections with fewer chapters) should be pruned."""
        remaining = self._get_remaining_tids(FFXIV_DURATIONS, FFXIV_CHAPTERS, play_all_tid=6)
        for tid in range(6):
            assert tid not in remaining, f"Title {tid} should be pruned as subset"

    def test_ffxiv_bonus_title_kept(self):
        """Title 7 (0 chapters, bonus content) should be kept."""
        remaining = self._get_remaining_tids(FFXIV_DURATIONS, FFXIV_CHAPTERS, play_all_tid=6)
        assert 7 in remaining

    def test_title_with_more_chapters_than_play_all_kept(self):
        """A title with more chapters than the play-all should not be pruned."""
        durations = {0: 10000, 1: 5000}  # 0 is play-all
        chapters = {0: 20, 1: 25}  # title 1 has MORE chapters
        remaining = self._get_remaining_tids(durations, chapters, play_all_tid=0)
        assert 1 in remaining

    def test_title_with_zero_chapters_kept(self):
        """Titles with 0 chapters should always be kept (unknown structure)."""
        durations = {0: 10000, 1: 300, 2: 500}
        chapters = {0: 20, 1: 0, 2: 5}
        remaining = self._get_remaining_tids(durations, chapters, play_all_tid=0)
        assert 1 in remaining  # 0 chapters → kept
        assert 2 not in remaining  # 5 chapters, subset → pruned
