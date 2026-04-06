"""Disc ID computation — DVD CRC64 and BD SHA256 fingerprints."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile


def _mount_disc_readonly(device: str) -> str | None:
    """Mount a disc read-only to a temp directory. Returns mount point or None.

    Tries UDF first (Blu-ray), then ISO 9660 (DVD), then auto-detect.
    Caller MUST call _unmount_disc() when done.
    """
    mount_point = tempfile.mkdtemp(prefix="strophalos-disc-")

    for fs_type in ("udf", "iso9660", "auto"):
        opts = ["mount", "-o", "ro"]
        if fs_type != "auto":
            opts += ["-t", fs_type]
        opts += [device, mount_point]

        result = subprocess.run(opts, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return mount_point

    os.rmdir(mount_point)
    return None


def _unmount_disc(mount_point: str) -> None:
    """Unmount and remove temp mount point."""
    subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
    try:
        os.rmdir(mount_point)
    except OSError:
        pass


def compute_dvd_disc_id(device: str) -> str | None:
    """Compute the standard DVD disc ID (CRC64 from VIDEO_TS IFO files).

    Uses the same algorithm as Windows IDvdInfo2::GetDiscID — compatible with
    pydvdid/dvdid databases. Requires mounting the disc to read IFO files.
    """
    try:
        from pydvdid_m import compute
    except ImportError:
        print("  DVD disc ID: pydvdid-m not installed, skipping")
        return None

    mount_point = _mount_disc_readonly(device)
    if not mount_point:
        print("  DVD disc ID: could not mount disc")
        return None

    try:
        disc_id = compute(mount_point)
        result = str(disc_id).lower()
        print(f"  DVD disc ID: {result}")
        return result
    except Exception as e:
        print(f"  DVD disc ID: computation failed: {e}")
        return None
    finally:
        _unmount_disc(mount_point)


def compute_bd_disc_id(device: str) -> str | None:
    """Compute a Blu-ray disc fingerprint.

    SHA-256 of the disc's index.bdmv + MovieObject.bdmv metadata files,
    which are not AACS-encrypted and uniquely identify the disc's content
    structure. Returns the hex digest or None.
    """
    mount_point = _mount_disc_readonly(device)
    if not mount_point:
        print("  BD disc ID: could not mount disc")
        return None

    try:
        bdmv_dir = os.path.join(mount_point, "BDMV")
        if not os.path.isdir(bdmv_dir):
            for entry in os.listdir(mount_point):
                if entry.upper() == "BDMV":
                    bdmv_dir = os.path.join(mount_point, entry)
                    break

        if not os.path.isdir(bdmv_dir):
            print("  BD disc ID: no BDMV directory found")
            return None

        h = hashlib.sha256()
        files_hashed = 0

        for fname in ("index.bdmv", "MovieObject.bdmv"):
            fpath = None
            for entry in os.listdir(bdmv_dir):
                if entry.lower() == fname.lower():
                    fpath = os.path.join(bdmv_dir, entry)
                    break

            if fpath and os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    h.update(f.read())
                files_hashed += 1

        if files_hashed == 0:
            print("  BD disc ID: no BDMV metadata files found")
            return None

        result = h.hexdigest()
        print(f"  BD disc ID: {result[:16]}... ({files_hashed} file(s) hashed)")
        return result
    except Exception as e:
        print(f"  BD disc ID: computation failed: {e}")
        return None
    finally:
        _unmount_disc(mount_point)


def compute_disc_id(media_type: str, device: str) -> str | None:
    """Compute disc ID based on media type. Returns hex string or None."""
    if media_type == "dvd":
        return compute_dvd_disc_id(device)
    elif media_type in ("bd", "uhd"):
        return compute_bd_disc_id(device)
    return None
