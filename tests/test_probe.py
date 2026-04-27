"""Tests for disc probe logic and DVD scanning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from strophalos.ripper.cdtoc import FullToc, TocEntry
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

    @patch("strophalos.ripper.probe._mount_disc", return_value=None)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=True)
    @patch("strophalos.ripper.probe._get_disc_id", return_value="cdx_id")
    @patch("strophalos.ripper.probe._get_disc_label", return_value="BEJEWELED_2")
    def test_cd_extra_unmountable_data_classified_via_toc(
        self, mock_label, mock_discid, mock_audio, mock_dvd, mock_mount
    ):
        """CD-Extra with non-FS data session: mount fails on every FS type,
        but the kernel TOC reports a data track → audio+data with the
        TOC-derived dd range so rip_data_disc can sparse-pad-extract."""
        # Bejeweled 2 Deluxe TOC fixture: data track at MB-offset 171781,
        # leadout at 315356. data_lba = 171631, sectors = 143575.
        toc = FullToc(
            first_track=1,
            last_track=18,
            leadout=315356,
            entries=[
                *(
                    TocEntry(track=i, offset=offs, is_data=False)
                    for i, offs in [
                        (1, 150),
                        (2, 8656),
                        (3, 19759),
                        (4, 20292),
                        (5, 25211),
                        (6, 33083),
                        (7, 39622),
                        (8, 48981),
                        (9, 59816),
                        (10, 73872),
                        (11, 85118),
                        (12, 103845),
                        (13, 105464),
                        (14, 117063),
                        (15, 127241),
                        (16, 136321),
                        (17, 148126),
                    ]
                ),
                TocEntry(track=18, offset=171781, is_data=True),
            ],
        )
        with patch("strophalos.ripper.probe.read_full_toc", return_value=toc):
            result = probe_disc("/dev/sr1")

        assert result.disc_type == "audio+data"
        assert result.has_audio is True
        assert result.has_data is True
        assert result.disc_id == "cdx_id"
        assert result.label == "BEJEWELED_2"
        assert result.data_lba == 171631
        assert result.data_sectors == 143575

    @patch("strophalos.ripper.probe._mount_disc", return_value=None)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=True)
    @patch("strophalos.ripper.probe._get_disc_id", return_value="audio_only_id")
    def test_pure_audio_when_toc_has_no_data_track(self, mock_discid, mock_audio, mock_dvd, mock_mount):
        """Audio tracks + mount fails + TOC has only audio → audio CD,
        not audio+data. Don't false-positive on plain audio CDs."""
        toc = FullToc(
            first_track=1,
            last_track=3,
            leadout=120000,
            entries=[
                TocEntry(track=1, offset=150, is_data=False),
                TocEntry(track=2, offset=40000, is_data=False),
                TocEntry(track=3, offset=80000, is_data=False),
            ],
        )
        with patch("strophalos.ripper.probe.read_full_toc", return_value=toc):
            result = probe_disc("/dev/sr1")

        assert result.disc_type == "audio"
        assert result.has_data is False
        assert result.data_lba == 0
        assert result.data_sectors == 0

    @patch("strophalos.ripper.probe._mount_disc", return_value=None)
    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=True)
    @patch("strophalos.ripper.probe._get_disc_id", return_value="x")
    def test_audio_when_toc_unreadable(self, mock_discid, mock_audio, mock_dvd, mock_mount):
        """If the TOC ioctl fails (returns None), fall through to plain audio
        — don't crash the probe."""
        with patch("strophalos.ripper.probe.read_full_toc", return_value=None):
            result = probe_disc("/dev/sr1")
        assert result.disc_type == "audio"

    @patch("strophalos.ripper.probe._check_video_dvd", return_value=(False, ""))
    @patch("strophalos.ripper.probe._check_audio_tracks", return_value=True)
    @patch("strophalos.ripper.probe._get_disc_id", return_value="x")
    def test_mountable_data_takes_precedence_over_toc_fallback(self, mock_discid, mock_audio, mock_dvd, tmp_path):
        """When the data session DOES mount, we use the mount path (not the
        TOC fallback) — the existing disc_type/data_lba=0 contract for
        kernel-exposed-at-sector-0 data sessions."""
        (tmp_path / "GAME.EXE").touch()
        with (
            patch("strophalos.ripper.probe._mount_disc", return_value=str(tmp_path)),
            patch("strophalos.ripper.probe._unmount"),
            patch("strophalos.ripper.probe.read_full_toc") as mock_toc,
        ):
            result = probe_disc("/dev/sr1")

        assert result.disc_type == "audio+data"
        # data_lba stays 0 — the dd path will use whole-device extraction
        assert result.data_lba == 0
        assert result.data_sectors == 0
        # TOC reader should not even have been called when mount succeeds
        mock_toc.assert_not_called()


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
