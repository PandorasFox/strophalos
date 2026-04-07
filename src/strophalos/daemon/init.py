"""One-time startup initialization — config dirs, device permissions, defaults."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path


def initialize(puid: int, pgid: int) -> None:
    """Run once at startup as root before dropping privileges."""
    from strophalos.cli.setup_key import main as setup_key

    # 0. Update CA certificates if any were mounted
    ca_cert = Path("/usr/local/share/ca-certificates/caddy-local.crt")
    if ca_cert.exists():
        subprocess.run(["update-ca-certificates"], capture_output=True, timeout=10)

    # 1. MakeMKV registration key
    setup_key()

    # 2. Config directories
    for d in ("/config/hooks", "/config/.config/whipper", "/config/.MakeMKV"):
        os.makedirs(d, exist_ok=True)

    # Symlink for MakeMKV compat
    try:
        os.symlink("/config", "/config/.MakeMKV", target_is_directory=True)
    except (FileExistsError, OSError):
        pass

    # 3. Device permissions
    for pattern in ("/dev/sr*", "/dev/sg*"):
        for dev in glob.glob(pattern):
            try:
                os.chmod(dev, 0o666)
            except OSError:
                pass

    # 4. Directory ownership
    for d in ("/config", "/output-cd"):
        try:
            _chown_recursive(d, puid, pgid)
        except OSError:
            pass

    archive_dirs = [
        "/media/archive/tv/rips",
        "/media/archive/movies/rips",
        "/media/archive/music/rips",
        "/media/archive/iso",
        "/media/tv",
        "/media/movies",
        "/media/music",
    ]
    for d in archive_dirs:
        os.makedirs(d, exist_ok=True)
        try:
            os.chown(d, puid, pgid)
        except OSError:
            pass

    # 5. Seed whipper config
    whipper_conf = Path("/config/.config/whipper/whipper.conf")
    default_conf = Path("/defaults/whipper.conf")
    if not whipper_conf.exists() and default_conf.exists():
        shutil.copy2(default_conf, whipper_conf)

    # 6. Seed hooks
    hooks_dir = Path("/defaults/hooks")
    if hooks_dir.is_dir():
        for hook in hooks_dir.iterdir():
            if hook.is_file():
                target = Path("/config/hooks") / hook.name
                if not target.exists():
                    shutil.copy2(hook, target)


def drop_privileges(puid: int, pgid: int) -> None:
    """Drop from root to the configured user. Call after initialize()."""
    if os.getuid() != 0:
        return  # Already non-root (e.g. running outside container)
    os.setgroups([])
    os.setgid(pgid)
    os.setuid(puid)


def _chown_recursive(path: str, uid: int, gid: int) -> None:
    """chown a directory and its immediate children."""
    os.chown(path, uid, gid)
    for entry in os.scandir(path):
        try:
            os.chown(entry.path, uid, gid)
        except OSError:
            pass
