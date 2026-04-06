"""PGS (Presentation Graphic Stream) subtitle parser.

Parses .sup files extracted from Blu-ray MKV containers. PGS subtitles are
bitmap-based — each subtitle is a rasterized image with palette and timing.

This module extracts (timestamp, PIL.Image) pairs for OCR processing.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

from PIL import Image

# PGS segment types
PCS = 0x16  # Presentation Composition Segment (timing)
WDS = 0x17  # Window Definition Segment
PDS = 0x14  # Palette Definition Segment
ODS = 0x15  # Object Definition Segment (bitmap data)
END = 0x80  # End of Display Set

HEADER_SIZE = 13  # "PG" (2) + PTS (4) + DTS (4) + type (1) + size (2)


def _read_header(f: BinaryIO) -> tuple[int, int, int, int] | None:
    """Read a PGS segment header. Returns (pts, dts, seg_type, data_len) or None at EOF."""
    buf = f.read(HEADER_SIZE)
    if len(buf) < HEADER_SIZE:
        return None
    magic = buf[0:2]
    if magic != b"PG":
        return None
    pts, dts = struct.unpack(">II", buf[2:10])
    seg_type = buf[10]
    data_len = struct.unpack(">H", buf[11:13])[0]
    return pts, dts, seg_type, data_len


def _parse_palette(data: bytes) -> dict[int, tuple[int, int, int, int]]:
    """Parse PDS data into {index: (R, G, B, A)} palette entries."""
    palette: dict[int, tuple[int, int, int, int]] = {}
    # Skip palette ID (2 bytes) and version (1 byte)
    entries = data[3:]
    for i in range(0, len(entries), 5):
        if i + 5 > len(entries):
            break
        idx = entries[i]
        y, cr, cb, a = entries[i + 1], entries[i + 2], entries[i + 3], entries[i + 4]
        # YCbCr → RGB
        r = max(0, min(255, int(y + 1.402 * (cr - 128))))
        g = max(0, min(255, int(y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128))))
        b = max(0, min(255, int(y + 1.772 * (cb - 128))))
        palette[idx] = (r, g, b, a)
    return palette


def _decode_rle(data: bytes, width: int, height: int) -> list[int]:
    """Decode PGS RLE-compressed bitmap to flat pixel index array."""
    pixels: list[int] = []
    pos = 0
    end = len(data)

    while pos < end and len(pixels) < width * height:
        byte = data[pos]
        pos += 1

        if byte != 0:
            pixels.append(byte)
        else:
            if pos >= end:
                break
            flag = data[pos]
            pos += 1

            if flag == 0:
                remaining = width - (len(pixels) % width) if len(pixels) % width != 0 else 0
                pixels.extend([0] * remaining)
            elif flag & 0xC0 == 0x00:
                count = flag & 0x3F
                pixels.extend([0] * count)
            elif flag & 0xC0 == 0x40:
                if pos >= end:
                    break
                count = ((flag & 0x3F) << 8) | data[pos]
                pos += 1
                pixels.extend([0] * count)
            elif flag & 0xC0 == 0x80:
                count = flag & 0x3F
                if pos >= end:
                    break
                color = data[pos]
                pos += 1
                pixels.extend([color] * count)
            elif flag & 0xC0 == 0xC0:
                if pos + 1 >= end:
                    break
                count = ((flag & 0x3F) << 8) | data[pos]
                pos += 1
                color = data[pos]
                pos += 1
                pixels.extend([color] * count)

    return pixels


def _bitmap_to_image(
    pixels: list[int],
    width: int,
    height: int,
    palette: dict[int, tuple[int, int, int, int]],
) -> Image.Image:
    """Convert palette-indexed pixel array to binary image for OCR.

    Keeps only bright pixels (alpha > 128), discarding outlines and
    anti-aliasing fringes.
    """
    img = Image.new("L", (width, height), 0)
    img_data = img.load()

    for y in range(height):
        for x in range(width):
            idx = y * width + x
            if idx < len(pixels):
                color_idx = pixels[idx]
                _r, _g, _b, a = palette.get(color_idx, (0, 0, 0, 0))
                if a > 128:
                    img_data[x, y] = 255

    return img


def extract_subtitle_images(
    sup_path: str | Path,
    max_pts: int | None = None,
) -> list[tuple[int, Image.Image]]:
    """Parse a PGS .sup file and extract subtitle images with timestamps.

    Args:
        sup_path: Path to the .sup file.
        max_pts: If set, stop after this PTS value (90kHz clock).
            Use duration_seconds * 90000 to limit by time.

    Returns:
        List of (pts_90khz, PIL.Image) pairs, sorted by timestamp.
    """
    results: list[tuple[int, Image.Image]] = []
    palette: dict[int, tuple[int, int, int, int]] = {}
    obj_data: bytes = b""
    obj_width: int = 0
    obj_height: int = 0
    current_pts: int = 0

    with open(sup_path, "rb") as f:
        while True:
            header = _read_header(f)
            if header is None:
                break

            pts, _dts, seg_type, data_len = header

            if max_pts is not None and pts > max_pts:
                f.seek(data_len, 1)
                continue

            data = f.read(data_len)
            if len(data) < data_len:
                break

            if seg_type == PCS:
                current_pts = pts

            elif seg_type == PDS:
                palette = _parse_palette(data)

            elif seg_type == ODS:
                if len(data) < 4:
                    continue
                seq_flag = data[3]

                if seq_flag & 0x80:  # First (or only) segment
                    if len(data) < 11:
                        continue
                    obj_width = struct.unpack(">H", data[7:9])[0]
                    obj_height = struct.unpack(">H", data[9:11])[0]
                    obj_data = data[11:]
                else:
                    obj_data += data[4:]

                if seq_flag & 0x40:  # Last segment — object complete
                    if obj_width > 0 and obj_height > 0 and palette:
                        pixels = _decode_rle(obj_data, obj_width, obj_height)
                        img = _bitmap_to_image(pixels, obj_width, obj_height, palette)
                        results.append((current_pts, img))

    results.sort(key=lambda x: x[0])
    return results
