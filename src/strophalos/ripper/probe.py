"""Disc type detection — layered probe without SCSI/makemkvcon.

Detection order (each step is fast and non-blocking):
1. cdparanoia -Q — audio CD (instant return on non-audio)
2. lsdvd — video DVD via libdvdread (handles CSS, no SCSI)
3. mount + check — Blu-ray (BDMV) or data disc
4. Unknown fallback

Nothing here uses SCSI or makemkvcon. DVD detection goes through
libdvdread which does standard block device reads with CSS decryption.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class ProbeResult:
    disc_type: str  # "audio", "dvd", "bluray", "data", "audio+data", "unknown"
    label: str
    disc_id: str  # audio CD disc ID, empty if no audio tracks
    has_audio: bool
    has_data: bool


def _check_audio_tracks(device: str) -> bool:
    """Check for audio tracks via cdparanoia -Q. Fast, never blocks."""
    try:
        result = subprocess.run(
            ["cdparanoia", "-Q", "-d", device],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "track" in result.stderr.lower()
    except Exception:
        return False


def _check_video_dvd(device: str) -> tuple[bool, str]:
    """Check for video DVD via lsdvd (libdvdread). Returns (is_dvd, label).

    Parses human-readable stderr output (lsdvd -Oj JSON support is unreliable).
    """
    try:
        result = subprocess.run(
            ["lsdvd", device],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # lsdvd writes disc info to stderr even on success
        output = result.stderr + result.stdout
        label = ""
        has_titles = False
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Disc Title:"):
                label = line.split(":", 1)[1].strip()
            if line.startswith("Title:"):
                has_titles = True
        if has_titles:
            return True, label
    except subprocess.TimeoutExpired:
        print("  Probe: lsdvd timed out", flush=True)
    except Exception:
        pass
    return False, ""


def _mount_disc(device: str) -> str | None:
    """Mount a disc read-only. Returns mount point or None.

    Tries UDF (Blu-ray), then ISO 9660 (data), then auto.
    Defensive umount between attempts — a failed UDF mount can leave
    the device/mount point in a state that blocks subsequent attempts.
    Caller MUST call _unmount() when done.
    """
    mount_point = tempfile.mkdtemp(prefix="strophalos-probe-")

    for fs_type in ("udf", "iso9660", "auto"):
        opts = ["mount", "-o", "ro"]
        if fs_type != "auto":
            opts += ["-t", fs_type]
        opts += [device, mount_point]

        result = subprocess.run(opts, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return mount_point

        print(f"  Probe: mount -t {fs_type} failed", flush=True)

        # Clean up after failed attempt — a partial/stale mount can block
        # the next filesystem type from succeeding.
        subprocess.run(["umount", mount_point], capture_output=True, timeout=5)

    os.rmdir(mount_point)
    return None


def _unmount(mount_point: str) -> None:
    subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
    try:
        os.rmdir(mount_point)
    except OSError:
        pass


def _get_disc_label(device: str) -> str:
    """Get filesystem label via blkid. Works without udev (unlike lsblk)."""
    try:
        result = subprocess.run(
            ["blkid", "-o", "value", "-s", "LABEL", device],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _has_dir(mount_point: str, name: str) -> bool:
    """Check if a directory exists at the mount point (case-insensitive)."""
    try:
        for entry in os.listdir(mount_point):
            if entry.upper() == name.upper():
                return os.path.isdir(os.path.join(mount_point, entry))
    except OSError:
        pass
    return False


def _get_disc_id(device: str) -> str:
    """Get MusicBrainz disc ID via libdiscid. Only call after confirming audio tracks."""
    try:
        import discid

        disc = discid.read(device)
        return disc.id
    except Exception:
        return ""


def probe_disc(device: str) -> ProbeResult:
    """Detect disc type without SCSI or makemkvcon."""

    # 1. Audio CD — cdparanoia returns instantly on non-audio
    print("  Probe: checking audio tracks (cdparanoia)...", flush=True)
    has_audio = _check_audio_tracks(device)

    # 2. Video DVD — lsdvd reads through libdvdread (handles CSS, no SCSI)
    print("  Probe: checking video DVD (lsdvd)...", flush=True)
    is_dvd, dvd_label = _check_video_dvd(device)

    if is_dvd:
        # DVDs don't have audio tracks in the CD sense
        print(f"  Probe: DVD detected, label='{dvd_label}'")
        return ProbeResult(disc_type="dvd", label=dvd_label, disc_id="", has_audio=False, has_data=True)

    if has_audio:
        # Could be pure audio or audio+data — try mounting to check for data
        print("  Probe: audio tracks found, checking for data partition...", flush=True)
        disc_id = _get_disc_id(device)
        mount_point = _mount_disc(device)
        if mount_point is not None:
            try:
                contents = os.listdir(mount_point)
                if contents:
                    label = _get_disc_label(device)
                    print(f"  Probe: audio+data disc ({len(contents)} data entries), label='{label}'")
                    return ProbeResult(
                        disc_type="audio+data", label=label, disc_id=disc_id, has_audio=True, has_data=True
                    )
            finally:
                _unmount(mount_point)

        print(f"  Probe: audio CD{f', disc_id={disc_id}' if disc_id else ''}")
        return ProbeResult(disc_type="audio", label="", disc_id=disc_id, has_audio=True, has_data=False)

    # 3. Not audio, not DVD — try mounting for Blu-ray or data disc
    print("  Probe: mounting disc (Blu-ray/data check)...", flush=True)
    mount_point = _mount_disc(device)
    if mount_point is not None:
        try:
            if _has_dir(mount_point, "BDMV"):
                label = _get_disc_label(device)
                print(f"  Probe: Blu-ray detected, label='{label}'")
                return ProbeResult(disc_type="bluray", label=label, disc_id="", has_audio=False, has_data=True)

            contents = os.listdir(mount_point)
            if contents:
                label = _get_disc_label(device)
                print(f"  Probe: data disc ({len(contents)} entries), label='{label}'")
                return ProbeResult(disc_type="data", label=label, disc_id="", has_audio=False, has_data=True)
        finally:
            _unmount(mount_point)

    # 4. Nothing worked
    print("  Probe: no audio, no DVD, mount failed — unknown")
    return ProbeResult(disc_type="unknown", label="", disc_id="", has_audio=False, has_data=False)
