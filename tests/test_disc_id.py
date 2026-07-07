"""Tests for DVD disc-id computation (ripper/disc_id.py)."""

from __future__ import annotations

import sys
import types

from strophalos.ripper.disc_id import compute_dvd_disc_id


class _FakeChecksum:
    def __str__(self) -> str:
        return "45F0F047|796B0D68"


def _install_fake_pydvdid(monkeypatch, record: dict, fail: bool = False):
    """Install a fake pydvdid_m exposing the real package's DvdId API."""

    class DvdId:
        def __init__(self, target):
            record["target"] = target
            if fail:
                raise FileNotFoundError("The /VIDEO_TS directory and it's files doesn't exist")
            self.checksum = _FakeChecksum()

    mod = types.ModuleType("pydvdid_m")
    mod.DvdId = DvdId
    monkeypatch.setitem(sys.modules, "pydvdid_m", mod)


class TestComputeDvdDiscId:
    def test_passes_device_path_directly_and_lowercases(self, monkeypatch):
        # The device path must go to DvdId verbatim (pycdlib reads the ISO
        # structures from the block device) — a mounted-directory path would
        # take DvdId's extracted-folder branch, which blocks on input().
        record: dict = {}
        _install_fake_pydvdid(monkeypatch, record)
        assert compute_dvd_disc_id("/dev/sr0") == "45f0f047|796b0d68"
        assert record["target"] == "/dev/sr0"

    def test_returns_none_on_computation_failure(self, monkeypatch):
        record: dict = {}
        _install_fake_pydvdid(monkeypatch, record, fail=True)
        assert compute_dvd_disc_id("/dev/sr0") is None

    def test_returns_none_when_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pydvdid_m", None)  # import raises
        assert compute_dvd_disc_id("/dev/sr0") is None
