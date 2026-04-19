"""CLI entry point for rip-cd — whipper wrapper with multi-disc detection.

Queries MusicBrainz via disc ID, detects multi-disc releases, adjusts
whipper output templates, and handles post-rip directory cleanup.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

from strophalos.core.notify import notify

# Linux CDROM ioctl — reads the full TOC including data tracks. libdiscid's
# discid.read() omits data tracks for multi-session discs, which produces an
# audio-only CDTOC that MB will refuse to attach to mixed-mode releases.
_CDROMREADTOCHDR = 0x5305
_CDROMREADTOCENTRY = 0x5306
_CDROM_LEADOUT = 0xAA
_CDROM_LBA = 0x01


def _read_full_toc(device: str) -> tuple[int, int, int, list[tuple[int, bool]]] | None:
    """Read the full CD TOC from the kernel, including data tracks.

    Returns (first_track, last_track, leadout_offset, [(track_offset, is_data), ...])
    with offsets in MB's sector convention (LBA + 150 for the 2-second lead-in).
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

        tracks = [_entry(t) for t in range(first, last + 1)]
        leadout, _ = _entry(_CDROM_LEADOUT)
        return first, last, leadout, tracks
    except OSError:
        return None
    finally:
        os.close(fd)


def _query_disc_info(device: str) -> dict:
    """Query MusicBrainz for disc metadata via disc ID.

    Returns dict with keys: disc_id, disc_total, title, artist, release_id, tracks.
    """
    info: dict = {"disc_total": 1}

    try:
        import discid
        import musicbrainzngs

        # Configure musicbrainzngs — always use upstream musicbrainz.org for
        # disc lookups. Mirrors lag behind and won't have freshly-submitted
        # releases/TOC attachments. The mirror (MB_SERVER) is used elsewhere
        # for non-time-sensitive queries like BD-OST classification.
        musicbrainzngs.set_useragent("rip-cd", "1.0", "local")
        musicbrainzngs.set_hostname("musicbrainz.org")

        disc = discid.read(device)

        # Mixed-mode override: if the kernel's full TOC contains a data track,
        # re-derive the disc ID from all tracks. MB stores mixed-mode CDTOCs
        # with the data track included, and the attach endpoint won't accept
        # the audio-only form libdiscid produces by default.
        toc = _read_full_toc(device)
        if toc is not None:
            first, last, leadout, entries = toc
            if any(is_data for _, is_data in entries):
                offsets = [off for off, _ in entries]
                disc = discid.put(first, last, leadout, offsets)

        info["disc_id"] = disc.id
        info["submission_url"] = disc.submission_url

        result = musicbrainzngs.get_releases_by_discid(disc.id, includes=["artists", "recordings", "release-groups"])

        if result.get("disc"):
            releases = result["disc"]["release-list"]
            if releases:
                rel = releases[0]
                release_detail = musicbrainzngs.get_release_by_id(
                    rel["id"], includes=["media", "discids", "recordings"]
                )["release"]

                info["disc_total"] = len(release_detail["medium-list"])
                info["title"] = rel["title"]
                info["artist"] = rel.get("artist-credit-phrase", "Unknown")
                info["release_id"] = rel["id"]

                # Build track list
                tracks = []
                for medium in release_detail.get("medium-list", []):
                    for track in medium.get("track-list", []):
                        rec = track.get("recording", {})
                        tracks.append(
                            {
                                "num": track.get("number", "?"),
                                "title": rec.get("title", "?"),
                                "id": rec.get("id", ""),
                            }
                        )
                if tracks:
                    info["tracks"] = tracks

    except Exception as e:
        import sys

        print(f"MusicBrainz query failed: {e}", file=sys.stderr)

    return info


def _flatten_multi_disc_dirs(output: Path) -> None:
    """Strip disc designations from top-level directory for multi-disc releases.

    e.g. "Artist - Album (Disc 1 of 2)/Disc 1/..." → "Artist - Album/Disc 1/..."
    """
    for d in sorted(output.iterdir()):
        if not d.is_dir():
            continue
        base = d.name
        clean = re.sub(r" *\(([Dd]isc|CD) \d+( of \d+)?\)$", "", base)
        if clean == base or not clean:
            continue
        target = output / clean
        target.mkdir(parents=True, exist_ok=True)
        for item in d.iterdir():
            shutil.move(str(item), str(target / item.name))
        try:
            d.rmdir()
        except OSError:
            pass


def rip_audio_cd(device: str, output: str = "/output-cd") -> int:
    """Core CD rip logic. Returns whipper exit code."""
    os.environ["HOME"] = "/config"

    print("Querying disc info...")
    info = _query_disc_info(device)

    disc_total = info.get("disc_total", 1)
    title = info.get("title", "")
    artist = info.get("artist", "")
    release_id = info.get("release_id", "")

    if disc_total > 1:
        print(f"Multi-disc release detected ({disc_total} discs), using disc number in path")
        track_tpl = "%A - %d/Disc %N/%t. %n"
        disc_tpl = "%A - %d/Disc %N/%A - %d"
    else:
        print("Single-disc release, using flat layout")
        track_tpl = "%A - %d/%t. %n"
        disc_tpl = "%A - %d/%A - %d"

    if title:
        notify("Ripping CD", f"{artist or 'Unknown'} - {title}")
    else:
        notify("Ripping CD", "(unknown disc)")

    # Run whipper (--cdr allows CD-R discs without extra prompting).
    # -R pins the release: whipper's own MB query uses an audio-only disc ID
    # which won't match mixed-mode submissions, so we hand it the release we
    # already found via the full-TOC disc ID.
    cmd = [
        "whipper",
        "cd",
        "-d",
        device,
        "rip",
        "--cdr",
        "-O",
        output,
        "--track-template",
        track_tpl,
        "--disc-template",
        disc_tpl,
    ]
    if release_id:
        cmd += ["-R", release_id]
    result = subprocess.run(cmd)
    rc = result.returncode

    # Post-rip: flatten multi-disc directories
    if disc_total > 1:
        _flatten_multi_disc_dirs(Path(output))

    # Post-rip: re-tag with English locale names from MusicBrainz
    if rc == 0:
        try:
            from strophalos.cli.retag_flac import retag_directory

            print("Re-tagging with English locale names...")
            retag_directory(Path(output))
        except Exception as e:
            print(f"  retag failed (non-fatal): {e}")

    # Notification
    if rc == 0 and title:
        lines = []
        mb_url = f"https://musicbrainz.org/release/{release_id}" if release_id else ""
        lines.append(f"{artist} - {title}")
        if mb_url:
            lines.append(mb_url)
        lines.append("")

        tracks = info.get("tracks", [])
        for t in tracks:
            lines.append(f"{t['num']}. {t['title']}")

        notify("CD ripped", "\n".join(lines))
    elif rc != 0:
        # Build the submission/attach URL from discid TOC data
        submission_url = info.get("submission_url", "")
        if submission_url:
            import re as _re

            attach_url = _re.sub(r"https?://[^/]+", "https://musicbrainz.org", submission_url)
        else:
            attach_url = ""

        msg = f"{artist or 'Unknown'} - {title or 'Unknown'} (exit {rc})"
        if attach_url:
            msg += f"\n\nSubmit/attach TOC:\n{attach_url}"
        notify("CD rip failed", msg, error=True)

    return rc


def main() -> None:
    parser = argparse.ArgumentParser(description="CD ripper — whipper with multi-disc detection")
    parser.add_argument("-d", "--device", default="/dev/sr1", help="CD drive device")
    parser.add_argument("-o", "--output", default="/output-cd", help="Output directory")
    args = parser.parse_args()

    rc = rip_audio_cd(args.device, args.output)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
