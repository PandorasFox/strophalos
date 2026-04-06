"""Notification dispatch via apprise.

Replaces notify.sh — direct Python call instead of shelling out.
Does nothing if NOTIFY_URL is not set (safe to call unconditionally).
"""

from __future__ import annotations

import os


def notify(title: str, body: str, *, error: bool = False) -> None:
    """Send a notification. No-op if NOTIFY_URL is unset."""
    url = os.environ.get("NOTIFY_URL", "")
    if not url:
        return

    try:
        import apprise

        ap = apprise.Apprise()
        ap.add(url)
        ap.notify(
            title=title,
            body=body,
            notify_type="failure" if error else "info",
        )
    except Exception as e:
        import sys

        print(f"notify: {e}", file=sys.stderr)
