"""Tests for OpenSubtitles AuthError — identification blocks on expired tokens."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from strophalos.backends.opensubtitles import AuthError, identify, fetch_reference_subs
from strophalos.types import Episode


def _expired_config():
    return {"api_key": "test-key", "token": "expired-token"}


class TestIdentifyAuthError:
    def test_raises_on_expired_token(self, tmp_path):
        mkv = tmp_path / "test_t00.mkv"
        mkv.write_bytes(b"\x00" * 131072)

        with (
            patch("strophalos.backends.opensubtitles._load_config", return_value=_expired_config()),
            patch("strophalos.backends.opensubtitles._token_valid", return_value=False),
        ):
            with pytest.raises(AuthError):
                identify([mkv])

    def test_no_error_when_unconfigured(self, tmp_path):
        mkv = tmp_path / "test_t00.mkv"
        mkv.write_bytes(b"\x00" * 131072)

        with patch("strophalos.backends.opensubtitles._load_config", return_value=None):
            result = identify([mkv])
        assert result is None


class TestFetchReferenceSubsAuthError:
    def test_raises_on_expired_token(self):
        episodes = [Episode(season=1, episode=1, title="Test", runtime_seconds=2600)]

        with (
            patch("strophalos.backends.opensubtitles._load_config", return_value=_expired_config()),
            patch("strophalos.backends.opensubtitles._token_valid", return_value=False),
        ):
            with pytest.raises(AuthError):
                fetch_reference_subs(episodes, series_id=1234)

    def test_returns_empty_when_unconfigured(self):
        episodes = [Episode(season=1, episode=1, title="Test", runtime_seconds=2600)]

        with patch("strophalos.backends.opensubtitles._load_config", return_value=None):
            result = fetch_reference_subs(episodes, series_id=1234)
        assert result == {}
