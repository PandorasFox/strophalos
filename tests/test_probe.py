"""Tests for disc probe logic and DVD scanning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from strophalos.ripper.probe import _has_dir, probe_disc


class TestHasDir:
    def test_finds_exact_case(self, tmp_path):
        (tmp_path / "VIDEO_TS").mkdir()
        assert _has_dir(str(tmp_path), "VIDEO_TS") is True

    def test_finds_case_insensitive(self, tmp_path):
        (tmp_path / "video_ts").mkdir()
        assert _has_dir(str(tmp_path), "VIDEO_TS") is True

    def test_missing(self, tmp_path):
        assert _has_dir(str(tmp_path), "VIDEO_TS") is False

    def test_file_not_dir(self, tmp_path):
        (tmp_path / "VIDEO_TS").touch()
        assert _has_dir(str(tmp_path), "VIDEO_TS") is False


class TestProbeDisc:
    @patch("strophalos.ripper.probe._mount_disc", return_value=None)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=True)
    @patch("strophalos.ripper.probe._get_disc_id", return_value="abc123")
    def test_audio_cd(self, mock_discid, mock_audio, mock_dvd, mock_mount):
        """cdparanoia finds tracks + lsdvd fails + mount fails → audio CD."""
        result = probe_disc("/dev/sr1")
        assert result.disc_type == "audio"
        assert result.disc_id == "abc123"
        assert result.has_audio is True
        assert result.has_data is False

    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=False)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(True, "MY_MOVIE"))
    def test_dvd_detected(self, mock_dvd, mock_audio):
        """lsdvd succeeds → DVD."""
        result = probe_disc("/dev/sr1")
        assert result.disc_type == "dvd"
        assert result.label == "MY_MOVIE"
        assert result.has_data is True
        assert result.has_audio is False

    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=False)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    def test_bluray_detected(self, mock_dvd, mock_audio, tmp_path):
        """Not audio, not DVD, mount + BDMV → Blu-ray."""
        (tmp_path / "BDMV").mkdir()

        lsblk_result = MagicMock(stdout="SHOW_S1\n")
        with (
            patch("strophalos.ripper.probe._mount_disc", return_value=str(tmp_path)),
            patch("strophalos.ripper.probe._unmount"),
            patch("strophalos.ripper.probe.subprocess.run", return_value=lsblk_result),
        ):
            result = probe_disc("/dev/sr1")

        assert result.disc_type == "bluray"
        assert result.label == "SHOW_S1"
        assert result.has_data is True

    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=False)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    def test_data_disc_detected(self, mock_dvd, mock_audio, tmp_path):
        """Not audio, not DVD, mount + no BDMV → data disc."""
        (tmp_path / "INSTALL.EXE").touch()

        lsblk_result = MagicMock(stdout="GAME_DISC\n")
        with (
            patch("strophalos.ripper.probe._mount_disc", return_value=str(tmp_path)),
            patch("strophalos.ripper.probe._unmount"),
            patch("strophalos.ripper.probe.subprocess.run", return_value=lsblk_result),
        ):
            result = probe_disc("/dev/sr1")

        assert result.disc_type == "data"
        assert result.has_audio is False
        assert result.has_data is True

    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=True)
    @patch("strophalos.ripper.probe._get_disc_id", return_value="disc123")
    def test_audio_plus_data_detected(self, mock_discid, mock_audio, mock_dvd, tmp_path):
        """Audio tracks + mountable data → audio+data."""
        (tmp_path / "GAME.EXE").touch()

        with (
            patch("strophalos.ripper.probe._mount_disc", return_value=str(tmp_path)),
            patch("strophalos.ripper.probe._unmount"),
        ):
            result = probe_disc("/dev/sr1")

        assert result.disc_type == "audio+data"
        assert result.has_audio is True
        assert result.has_data is True
        assert result.disc_id == "disc123"

    @patch("strophalos.ripper.probe._mount_disc", return_value=None)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=False)
    def test_unknown_when_nothing_works(self, mock_audio, mock_dvd, mock_mount):
        """No audio, no DVD, mount fails → unknown."""
        result = probe_disc("/dev/sr1")
        assert result.disc_type == "unknown"
        assert result.has_audio is False
        assert result.has_data is False


# ---------------------------------------------------------------------------
# DVD scan (lsdvd JSON parsing)
# ---------------------------------------------------------------------------


class TestDvdParseDuration:
    def test_basic(self):
        from strophalos.ripper.dvd import _parse_duration

        assert _parse_duration("01:14:42.467") == 4482
        assert _parse_duration("00:14:11.567") == 851
        assert _parse_duration("00:00:34.500") == 34
        assert _parse_duration("nope") == 0


class TestScanDvd:
    def test_parses_lsdvd_output(self):
        """scan_dvd should parse lsdvd human-readable output."""
        lsdvd_stderr = (
            "Disc Title: MY_MOVIE\n"
            "Title: 01, Length: 02:00:00.500 Chapters: 12, Cells: 12, Audio streams: 02, Subpictures: 01\n"
            "Title: 02, Length: 00:05:00.000 Chapters: 01, Cells: 01, Audio streams: 01, Subpictures: 00\n"
            "Title: 03, Length: 00:03:00.000 Chapters: 01, Cells: 01, Audio streams: 01, Subpictures: 00\n"
            "Longest track: 01\n"
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = lsdvd_stderr

        with patch("strophalos.ripper.dvd.subprocess.run", return_value=mock_result):
            from strophalos.ripper.dvd import scan_dvd

            label, durations, chapters = scan_dvd("/dev/sr1")

        assert label == "MY_MOVIE"
        assert durations == {0: 7200, 1: 300, 2: 180}
        assert chapters == {0: 12, 1: 1, 2: 1}

    def test_handles_lsdvd_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "no disc"

        with patch("strophalos.ripper.dvd.subprocess.run", return_value=mock_result):
            from strophalos.ripper.dvd import scan_dvd

            label, durations, chapters = scan_dvd("/dev/sr1")

        assert label is None
        assert durations == {}
        assert chapters == {}
