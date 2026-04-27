"""Tests for kernel CDROM TOC reader (strophalos.ripper.cdtoc)."""

from __future__ import annotations

from strophalos.ripper.cdtoc import FullToc, TocEntry


def _bj2_toc() -> FullToc:
    """Bejeweled 2 Deluxe Soundtrack — CD-Extra (data track last).

    Captured from the live disc on 2026-04-27. Used as the canonical
    CD-Extra fixture: 17 audio tracks, track 18 is data at LBA 171631.
    """
    audio_offsets = [
        150,
        8656,
        19759,
        20292,
        25211,
        33083,
        39622,
        48981,
        59816,
        73872,
        85118,
        103845,
        105464,
        117063,
        127241,
        136321,
        148126,
    ]
    entries = [TocEntry(track=i + 1, offset=off, is_data=False) for i, off in enumerate(audio_offsets)]
    entries.append(TocEntry(track=18, offset=171781, is_data=True))
    return FullToc(first_track=1, last_track=18, leadout=315356, entries=entries)


def _mixed_mode_toc() -> FullToc:
    """Older-style mixed-mode CD: track 1 is data, audio follows."""
    return FullToc(
        first_track=1,
        last_track=4,
        leadout=200000,
        entries=[
            TocEntry(track=1, offset=150, is_data=True),
            TocEntry(track=2, offset=50000, is_data=False),
            TocEntry(track=3, offset=100000, is_data=False),
            TocEntry(track=4, offset=150000, is_data=False),
        ],
    )


def _pure_audio_toc() -> FullToc:
    return FullToc(
        first_track=1,
        last_track=3,
        leadout=120000,
        entries=[
            TocEntry(track=1, offset=150, is_data=False),
            TocEntry(track=2, offset=40000, is_data=False),
            TocEntry(track=3, offset=80000, is_data=False),
        ],
    )


class TestHasDataTrack:
    def test_cd_extra_detected(self):
        assert _bj2_toc().has_data_track() is True

    def test_mixed_mode_detected(self):
        assert _mixed_mode_toc().has_data_track() is True

    def test_pure_audio_negative(self):
        assert _pure_audio_toc().has_data_track() is False


class TestDataTrackAccessor:
    def test_returns_last_track_for_cd_extra(self):
        dt = _bj2_toc().data_track()
        assert dt is not None
        assert dt.track == 18
        assert dt.offset == 171781
        assert dt.is_data is True

    def test_returns_first_track_for_mixed_mode(self):
        dt = _mixed_mode_toc().data_track()
        assert dt is not None
        assert dt.track == 1

    def test_none_for_pure_audio(self):
        assert _pure_audio_toc().data_track() is None


class TestOffsets:
    def test_returns_all_offsets_in_track_order(self):
        offsets = _bj2_toc().offsets()
        assert len(offsets) == 18
        assert offsets[0] == 150  # track 1
        assert offsets[-1] == 171781  # track 18 (data)


class TestRealDiscInvariants:
    """Sanity-check the TocEntry offsets correspond to the values the
    rip-data path uses to derive the dd skip/count for CD-Extra discs."""

    def test_data_lba_is_offset_minus_pregap(self):
        toc = _bj2_toc()
        dt = toc.data_track()
        assert dt is not None
        # MB convention: offset = LBA + 150 (2-second pregap)
        data_lba = dt.offset - 150
        assert data_lba == 171631

    def test_data_track_sectors_to_leadout(self):
        toc = _bj2_toc()
        dt = toc.data_track()
        assert dt is not None
        data_lba = dt.offset - 150
        leadout_lba = toc.leadout - 150
        sectors = leadout_lba - data_lba
        # Verified live: 143575 sectors = 280 MB at 2048 b/s
        assert sectors == 143575
