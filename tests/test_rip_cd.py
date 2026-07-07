"""Tests for CD rip failure classification (cli/rip_cd.py)."""

from __future__ import annotations

from strophalos.cli.rip_cd import _checksum_failed_tracks

# Actual daemon log excerpt from a disc that needed cleaning (PFR - Them)
_WHIPPER_LOG = [
    "INFO:whipper.program.cdparanoia:checksums do not match, 2f5ea307 4d0a9c7f\n",
    "INFO:whipper.command.cd:ripping track 8 of 12 (try 2): 08. Face to Face.flac\n",
    "INFO:whipper.program.cdparanoia:checksums do not match, 91b19e5a 9f1902ce\n",
    "INFO:whipper.command.cd:ripping track 8 of 12 (try 3): 08. Face to Face.flac\n",
    "INFO:whipper.program.cdparanoia:checksums do not match, a5f29359 04b4a825\n",
    "INFO:whipper.command.cd:ripping track 8 of 12 (try 4): 08. Face to Face.flac\n",
    "INFO:whipper.program.cdparanoia:checksums do not match, c89f24f1 05821463\n",
    "INFO:whipper.command.cd:ripping track 8 of 12 (try 5): 08. Face to Face.flac\n",
    "INFO:whipper.program.cdparanoia:checksums do not match, 8a252ea5 699fad44\n",
    "CRITICAL:whipper.command.cd:giving up on track 8 after 5 times\n",
    "track can't be ripped. Rip attempts number is equal to 5\n",
]


class TestChecksumFailedTracks:
    def test_detects_given_up_track(self):
        assert _checksum_failed_tracks(_WHIPPER_LOG) == [8]

    def test_multiple_tracks_deduped_and_sorted(self):
        log = [
            "CRITICAL:whipper.command.cd:giving up on track 11 after 5 times\n",
            "CRITICAL:whipper.command.cd:giving up on track 3 after 5 times\n",
            "CRITICAL:whipper.command.cd:giving up on track 3 after 5 times\n",
        ]
        assert _checksum_failed_tracks(log) == [3, 11]

    def test_checksum_retries_alone_are_not_failures(self):
        # Retries that eventually converge shouldn't classify as failure
        log = [
            "INFO:whipper.program.cdparanoia:checksums do not match, 2f5ea307 4d0a9c7f\n",
            "INFO:whipper.command.cd:ripping track 8 of 12 (try 2): 08. Face to Face.flac\n",
        ]
        assert _checksum_failed_tracks(log) == []

    def test_empty_log(self):
        assert _checksum_failed_tracks([]) == []
