"""CLI entry point for setup-opensubtitles — authenticate with OpenSubtitles API."""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

from strophalos.core.http import post_json

OPENSUBTITLES_API_BASE = "https://api.opensubtitles.com/api/v1"
CONFIG_PATH = Path("/config/opensubtitles.json")


def main() -> None:
    api_key = os.environ.get("OPENSUBTITLES_API_KEY", "")
    if not api_key:
        print("OPENSUBTITLES_API_KEY not set. Get one at https://www.opensubtitles.com/consumers")
        sys.exit(1)

    print("OpenSubtitles authentication")
    print("=" * 40)

    username = input("Username: ").strip()
    if not username:
        print("Username required")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("Password required")
        sys.exit(1)

    print("\nAuthenticating...")
    data = post_json(
        f"{OPENSUBTITLES_API_BASE}/login",
        {"username": username, "password": password},
        headers={
            "Api-Key": api_key,
            "User-Agent": "strophalos v1.0",
        },
    )

    if not data:
        print("Login failed — check credentials and API key")
        sys.exit(1)

    token = data.get("token", "")
    if not token:
        print(f"No token in response: {data}")
        sys.exit(1)

    config = {
        "username": username,
        "token": token,
        "base_url": data.get("base_url", ""),
    }

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    print(f"\nAuthenticated as {username}")
    print(f"Token saved to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
