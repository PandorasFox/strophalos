"""Disc drive operations — SCSI readcap presence check, eject, USB reset.

Uses /dev/sgN (SCSI generic) for drive queries where possible — this
bypasses the block layer and avoids uninterruptible kernel sleep (D-state)
that can occur when tools open /dev/srN on a drive with tray open.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time

# Linux ioctl for CDROM_DRIVE_STATUS
CDROM_DRIVE_STATUS = 0x5326

CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4


def _find_sg_device(sr_device: str) -> str | None:
    """Find the /dev/sgN corresponding to /dev/srN.

    Falls back to /dev/sg0 if we can't resolve it.
    """
    # Try /sys/block/sr0/device/scsi_generic/sg*
    sr_name = os.path.basename(sr_device)
    sg_dir = f"/sys/block/{sr_name}/device/scsi_generic"
    try:
        for entry in os.listdir(sg_dir):
            if entry.startswith("sg"):
                return f"/dev/{entry}"
    except OSError:
        pass
    return None


def check_disc_present(device: str) -> bool:
    """Check if a disc is loaded by reading capacity via SCSI generic device.

    Uses sg_readcap on /dev/sgN — goes through SCSI generic driver,
    never triggers block layer I/O, never causes D-state.
    Returns True only if disc reports size > 0.
    """
    sg = _find_sg_device(device)
    if sg is None:
        # Fallback: ioctl on sr device (less safe but better than nothing)
        return get_drive_status(device) == CDS_DISC_OK

    try:
        result = subprocess.run(
            ["sg_readcap", sg],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        # Parse "Device size: 736667648 bytes" or "Device size: 0 bytes"
        m = re.search(r"Device size:\s+(\d+)\s+bytes", result.stdout)
        if m:
            return int(m.group(1)) > 0
        return False
    except Exception:
        return False


def get_drive_status(device: str) -> int:
    """Get raw drive status via ioctl. Returns -1 on error.

    Note: unreliable on some drives (e.g. ASUS BW-16D1HT reports
    CDS_DISC_OK with tray open). Prefer check_disc_present().
    """
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, CDROM_DRIVE_STATUS)
        finally:
            os.close(fd)
    except Exception:
        return -1


def eject_disc(device: str) -> bool:
    """Eject disc via /usr/bin/eject. Returns True on success."""
    result = subprocess.run(["eject", device], capture_output=True, timeout=30)
    return result.returncode == 0


def check_media_changed(device: str) -> bool:
    """Check if media has changed since last call via CDROM_MEDIA_CHANGED.

    Returns True if media was swapped. Resets on read (single-consumer).
    """
    CDROM_MEDIA_CHANGED = 0x5325
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, CDROM_MEDIA_CHANGED, 0) == 1
        finally:
            os.close(fd)
    except Exception:
        return False


def reset_usb_device(usb_port: str) -> None:
    """Unbind and rebind a USB device to reset it."""
    unbind = "/sys/bus/usb/drivers/usb/unbind"
    bind = "/sys/bus/usb/drivers/usb/bind"

    if not os.access(unbind, os.W_OK):
        print("[strophalos] USB reset: sysfs not writable")
        return

    print(f"[strophalos] USB reset: unbinding {usb_port}...")
    try:
        with open(unbind, "w") as f:
            f.write(usb_port)
    except OSError:
        pass
    time.sleep(2)

    print(f"[strophalos] USB reset: rebinding {usb_port}...")
    try:
        with open(bind, "w") as f:
            f.write(usb_port)
    except OSError:
        pass
    time.sleep(2)

    print("[strophalos] USB reset: done")
