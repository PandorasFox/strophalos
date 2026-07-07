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
import sys
from pathlib import Path

from strophalos.core.notify import notify
from strophalos.ripper.cdtoc import read_full_toc

# whipper gives up on a track after N rip attempts whose cdparanoia
# checksums never converge — the signature of a dirty/scratched disc.
_GIVE_UP_RE = re.compile(r"giving up on track (\d+) after")


def _checksum_failed_tracks(log_lines: list[str]) -> list[int]:
    """Track numbers whipper gave up on (repeated checksum mismatches)."""
    return sorted({int(m.group(1)) for line in log_lines if (m := _GIVE_UP_RE.search(line))})


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

        # Mixed-mode override (data track at position 1): MB stores these
        # CDTOCs with the data track included, so re-derive the disc ID from
        # all tracks. CD-Extra (data at end) follows the standard MB
        # convention — audio-only disc ID with data tracks attached
        # separately via the "data tracks at the end" flag — so leave
        # libdiscid's default behavior alone there.
        toc = read_full_toc(device)
        if toc is not None and toc.entries and toc.entries[0].is_data:
            disc = discid.put(toc.first_track, toc.last_track, toc.leadout, toc.offsets())

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


def rip_audio_cd(device: str, output: str = "/output-cd") -> tuple[int, str]:
    """Core CD rip logic. Returns (whipper exit code, output dir).

    On success the second element is the rip's parent output dir (the actual
    rip lives in an ``{artist} - {title}/`` subdirectory whipper names from
    its templates). On failure it's the empty string so callers can treat
    falsy = failure.
    """
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
        notify("Ripping CD", f"{artist or 'Unknown'} - {title}", dedup=False)
    else:
        notify("Ripping CD", "(unknown disc)", dedup=False)

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
    # Tee whipper's stderr (its logging stream) so rip-failure modes can be
    # classified from the log afterwards; stdout passes through untouched.
    whipper_log: list[str] = []
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
    assert proc.stderr is not None
    for line in proc.stderr:
        sys.stderr.write(line)
        whipper_log.append(line)
    rc = proc.wait()

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

        notify("CD ripped", "\n".join(lines), dedup=False)
    elif rc != 0:
        disc_name = f"{artist or 'Unknown'} - {title or 'Unknown'}"
        failed_tracks = _checksum_failed_tracks(whipper_log)
        if failed_tracks:
            # Read errors, not a metadata problem — the TOC attach URL would
            # just be noise here.
            tracks_str = ", ".join(str(t) for t in failed_tracks)
            plural = "s" if len(failed_tracks) > 1 else ""
            notify(
                "CD rip failed — track checksum failure",
                f"{disc_name}\nTrack{plural} {tracks_str}: checksums never converged "
                "after repeated reads.\nClean the disc and re-insert to retry.",
                error=True,
                dedup=False,
            )
        else:
            # Build the submission/attach URL from discid TOC data
            submission_url = info.get("submission_url", "")
            if submission_url:
                attach_url = re.sub(r"https?://[^/]+", "https://musicbrainz.org", submission_url)
            else:
                attach_url = ""

            msg = f"{disc_name} (exit {rc})"
            if attach_url:
                msg += f"\n\nSubmit/attach TOC:\n{attach_url}"
            notify("CD rip failed", msg, error=True, dedup=False)

    return rc, (output if rc == 0 else "")


def main() -> None:
    parser = argparse.ArgumentParser(description="CD ripper — whipper with multi-disc detection")
    parser.add_argument("-d", "--device", default="/dev/sr1", help="CD drive device")
    parser.add_argument("-o", "--output", default="/output-cd", help="Output directory")
    args = parser.parse_args()

    rc, _ = rip_audio_cd(args.device, args.output)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
