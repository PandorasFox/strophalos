"""Tests for rip_data_disc — focusing on the CD-Extra sparse-pad flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from strophalos.cli.rip_data import rip_data_disc


def _fake_dd(cmd, *args, **kwargs):
    """Fake subprocess.run for dd — touch the output file so the
    rip-success post-processing (size check, sidecar write) can run."""
    of_path = next(a for a in cmd if a.startswith("of=")).split("=", 1)[1]
    # Write a small placeholder so getsize() returns something reasonable.
    with open(of_path, "wb") as f:
        f.write(b"\x00" * 1024)
    return MagicMock(returncode=0)


class TestRipDataDiscPlainDataDisc:
    """skip_sectors=0 is the legacy plain-data-disc path: whole-device dd,
    no sidecar."""

    def test_dd_command_has_no_skip_or_seek(self, tmp_path):
        with patch("strophalos.cli.rip_data.subprocess.run", side_effect=_fake_dd) as run:
            result = rip_data_disc("/dev/sr0", label="game", output=str(tmp_path))

        assert result is not None
        cmd = run.call_args.args[0]
        assert cmd[0] == "dd"
        assert "if=/dev/sr0" in cmd
        assert any(a.startswith("of=") for a in cmd)
        # No CD-Extra flags
        assert not any(a.startswith("skip=") for a in cmd)
        assert not any(a.startswith("seek=") for a in cmd)
        assert "conv=sparse" not in cmd

    def test_no_sidecar_for_plain_data_disc(self, tmp_path):
        with patch("strophalos.cli.rip_data.subprocess.run", side_effect=_fake_dd):
            rip_data_disc("/dev/sr0", label="game", output=str(tmp_path))

        sidecars = list(tmp_path.glob("*.mount-info"))
        assert sidecars == []


class TestRipDataDiscCdExtra:
    """skip_sectors > 0 → sparse-pad layout for CD-Extra discs whose data
    session uses absolute-disc LBAs in its ISO9660 records."""

    def test_dd_command_has_skip_seek_and_sparse(self, tmp_path):
        with patch("strophalos.cli.rip_data.subprocess.run", side_effect=_fake_dd) as run:
            result = rip_data_disc(
                "/dev/sr0",
                label="bejeweled2",
                output=str(tmp_path),
                skip_sectors=171631,
                count_sectors=143575,
            )

        assert result is not None
        cmd = run.call_args.args[0]
        # skip and seek must match — that's what produces the absolute-LBA
        # alignment so the data lands at file offset skip*2048.
        assert "skip=171631" in cmd
        assert "seek=171631" in cmd
        assert "conv=sparse" in cmd
        assert "count=143575" in cmd

    def test_sidecar_written_with_sbsector(self, tmp_path):
        with patch("strophalos.cli.rip_data.subprocess.run", side_effect=_fake_dd):
            rip_data_disc(
                "/dev/sr0",
                label="bejeweled2",
                output=str(tmp_path),
                skip_sectors=171631,
                count_sectors=143575,
            )

        sidecar = tmp_path / "bejeweled2.iso.mount-info"
        assert sidecar.exists()
        body = sidecar.read_text()
        assert "sbsector=171631" in body
        assert "data_track_lba=171631" in body
        assert "data_track_sectors=143575" in body
        # Includes the literal mount command so users don't have to
        # remember the sbsector convention.
        assert "mount -t iso9660 -o loop,ro,sbsector=171631" in body

    def test_no_sidecar_on_dd_failure(self, tmp_path):
        def fail(cmd, *args, **kwargs):
            of_path = next(a for a in cmd if a.startswith("of=")).split("=", 1)[1]
            # dd creates the output file before failing, mimicking real dd
            open(of_path, "wb").close()
            return MagicMock(returncode=1)

        with patch("strophalos.cli.rip_data.subprocess.run", side_effect=fail):
            result = rip_data_disc(
                "/dev/sr0",
                label="bejeweled2",
                output=str(tmp_path),
                skip_sectors=171631,
                count_sectors=143575,
            )

        assert result is None
        assert list(tmp_path.glob("*.mount-info")) == []
        # Failed-rip ISO should be cleaned up, not left as a corrupt artifact
        assert list(tmp_path.glob("*.iso")) == []

    def test_filename_safe_label(self, tmp_path):
        """Labels with slashes/spaces get sanitized in both the iso filename
        and the sidecar filename."""
        with patch("strophalos.cli.rip_data.subprocess.run", side_effect=_fake_dd):
            rip_data_disc(
                "/dev/sr0",
                label="Some / Disc",
                output=str(tmp_path),
                skip_sectors=100,
                count_sectors=200,
            )

        # Slashes → _, spaces → _
        iso = tmp_path / "Some___Disc.iso"
        sidecar = tmp_path / "Some___Disc.iso.mount-info"
        assert iso.exists()
        assert sidecar.exists()


class TestDryRun:
    def test_dry_run_does_not_invoke_dd_or_write_sidecar(self, tmp_path):
        with patch("strophalos.cli.rip_data.subprocess.run") as run:
            result = rip_data_disc(
                "/dev/sr0",
                label="x",
                output=str(tmp_path),
                dry_run=True,
                skip_sectors=171631,
                count_sectors=143575,
            )
        assert result is not None
        run.assert_not_called()
        assert list(tmp_path.glob("*")) == []
