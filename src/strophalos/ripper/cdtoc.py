"""Kernel CDROM TOC reader — full TOC including data tracks.

libdiscid's ``discid.read()`` omits data tracks for multi-session discs,
which is wrong for our purposes (probe classification, mixed-mode disc IDs).
This module reads the raw TOC via the Linux CDROM ioctls instead.
"""

from __future__ import annotations

import fcntl
import os
import struct
from dataclasses import dataclass

_CDROMREADTOCHDR = 0x5305
_CDROMREADTOCENTRY = 0x5306
_CDROM_LEADOUT = 0xAA
_CDROM_LBA = 0x01


@dataclass
class TocEntry:
    """A single TOC entry. ``offset`` follows MB convention (LBA + 150)."""

    track: int
    offset: int
    is_data: bool


@dataclass
class FullToc:
    first_track: int
    last_track: int
    leadout: int  # MB-convention offset (LBA + 150)
    entries: list[TocEntry]

    def has_data_track(self) -> bool:
        return any(e.is_data for e in self.entries)

    def offsets(self) -> list[int]:
        return [e.offset for e in self.entries]

    def data_track(self) -> TocEntry | None:
        for e in self.entries:
            if e.is_data:
                return e
        return None


def read_full_toc(device: str) -> FullToc | None:
    """Read the full CD TOC from the kernel, including data tracks.

    Returns None if the device can't be opened or the ioctl fails.
    """
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        hdr = bytearray(2)
        fcntl.ioctl(fd, _CDROMREADTOCHDR, hdr, True)
        first, last = hdr[0], hdr[1]

        def _entry(track_num: int) -> tuple[int, bool]:
            # struct cdrom_tocentry: track(1) adr_ctrl(1) format(1) pad(1) lba(4) datamode(1) pad(3) = 12
            buf = bytearray(12)
            buf[0] = track_num & 0xFF
            buf[2] = _CDROM_LBA
            fcntl.ioctl(fd, _CDROMREADTOCENTRY, buf, True)
            lba = struct.unpack("<i", bytes(buf[4:8]))[0]
            # Kernel packs bitfield as adr:4 (low nibble) | ctrl:4 (high nibble).
            # Data-track flag is ctrl & 0x04, i.e. byte & 0x40.
            is_data = bool(buf[1] & 0x40)
            return lba + 150, is_data

        entries: list[TocEntry] = []
        for t in range(first, last + 1):
            offset, is_data = _entry(t)
            entries.append(TocEntry(track=t, offset=offset, is_data=is_data))
        leadout, _ = _entry(_CDROM_LEADOUT)
        return FullToc(first_track=first, last_track=last, leadout=leadout, entries=entries)
    except OSError:
        return None
    finally:
        os.close(fd)
