"""CLI entry point for rip-cd — whipper wrapper with multi-disc detection.

Queries MusicBrainz via disc ID, detects multi-disc releases, adjusts
whipper output templates, and handles post-rip directory cleanup.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

from strophalos.core.notify import notify


def _query_disc_info(device: str) -> dict:
    """Query MusicBrainz for disc metadata via disc ID.

    Returns dict with keys: disc_id, disc_total, title, artist, release_id, tracks.
    """
    info: dict = {"disc_total": 1}

    try:
        import discid
        import musicbrainzngs

        # Configure musicbrainzngs
        musicbrainzngs.set_useragent("rip-cd", "1.0", "local")

        # Use MB_SERVER env var, fall back to whipper config, then default
        mb_server = os.environ.get("MB_SERVER", "")
        if mb_server:
            mb_server = mb_server.replace("http://", "").replace("https://", "")
            musicbrainzngs.set_hostname(mb_server)
        else:
            try:
                from whipper.common.config import Config

                conf = Config()
                server = conf._parser.get("musicbrainz", "server")
                server = server.replace("http://", "").replace("https://", "")
                musicbrainzngs.set_hostname(server)
            except Exception:
                pass

        disc = discid.read(device)
        info["disc_id"] = disc.id

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

    # Run whipper
    cmd = [
        "whipper",
        "cd",
        "-d",
        device,
        "rip",
        "-O",
        output,
        "--track-template",
        track_tpl,
        "--disc-template",
        disc_tpl,
    ]
    result = subprocess.run(cmd)
    rc = result.returncode

    # Post-rip: flatten multi-disc directories
    if disc_total > 1:
        _flatten_multi_disc_dirs(Path(output))

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
        notify("CD rip failed", f"{artist or 'Unknown'} - {title or 'Unknown'} (exit {rc})", error=True)

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
