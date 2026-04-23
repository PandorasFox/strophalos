"""Notification dispatch via apprise.

Replaces notify.sh — direct Python call instead of shelling out.
Does nothing if NOTIFY_URL is not set (safe to call unconditionally).

Deduplication: identical (title, body) pairs are suppressed until the cache
is full and the oldest entry is evicted.  Physical events (rip start/end/fail)
should pass ``dedup=False`` to always send.
"""

from __future__ import annotations

import os
from collections import OrderedDict

# LRU dedup cache — holds the last N unique messages
_sent: OrderedDict[tuple[str, str], None] = OrderedDict()
_CACHE_SIZE = 64


def notify(title: str, body: str, *, error: bool = False, dedup: bool = True) -> None:
    """Send a notification. No-op if NOTIFY_URL is unset.

    When *dedup* is True (default), identical (title, body) messages are
    suppressed until evicted from the LRU cache.  Pass ``dedup=False``
    for physically-triggered events (disc inserted, rip started, etc.)
    that should always send.
    """
    url = os.environ.get("NOTIFY_URL", "")
    if not url:
        return

    if dedup:
        key = (title, body)
        if key in _sent:
            _sent.move_to_end(key)
            return
        _sent[key] = None
        if len(_sent) > _CACHE_SIZE:
            _sent.popitem(last=False)

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
