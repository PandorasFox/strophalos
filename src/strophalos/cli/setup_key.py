"""CLI entry point for setup-key — configure MakeMKV registration key."""

from __future__ import annotations

import os
import re
from pathlib import Path


def main() -> None:
    key = os.environ.get("MAKEMKV_KEY", "")
    if not key:
        return

    config_dir = Path("/config/.MakeMKV")
    config_dir.mkdir(parents=True, exist_ok=True)

    settings = Path("/config/settings.conf")
    key_line = f'app_Key = "{key}"'

    if settings.exists():
        text = settings.read_text()
        if re.search(r"^app_Key", text, re.MULTILINE):
            text = re.sub(r"^app_Key.*$", key_line, text, flags=re.MULTILINE)
            settings.write_text(text)
            return

    # Append key
    with open(settings, "a") as f:
        f.write(f"{key_line}\n")


if __name__ == "__main__":
    main()
