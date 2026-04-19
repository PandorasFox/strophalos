"""Simple JSON disk cache for expensive API calls."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_MISS = object()
_DEFAULT_DIR = Path("/config/cache")


class DiskCache:
    """Key-value cache backed by a single JSON file per namespace.

    Entries expire after *ttl_days* (default 90).  A cached ``None`` is
    distinct from a cache miss — useful for remembering "no results" so
    we don't re-query a paid API.
    """

    def __init__(
        self,
        name: str,
        *,
        ttl_days: int = 90,
        cache_dir: Path = _DEFAULT_DIR,
    ) -> None:
        self.path = cache_dir / f"{name}.json"
        self.ttl = ttl_days * 86400
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        return self._data

    def get(self, key: str) -> Any:
        """Return cached value, or *_MISS* sentinel on miss / expiry."""
        data = self._load()
        entry = data.get(key)
        if entry is None:
            return _MISS
        if time.time() - entry["ts"] > self.ttl:
            del data[key]
            self._flush()
            return _MISS
        return entry["v"]

    def put(self, key: str, value: Any) -> None:
        """Store *value* (including ``None``) under *key*."""
        data = self._load()
        data[key] = {"ts": time.time(), "v": value}
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, separators=(",", ":")))
        tmp.rename(self.path)
