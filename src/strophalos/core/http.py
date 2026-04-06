"""Shared HTTP utilities — typed urllib wrapper for JSON APIs."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> dict[str, Any] | None:
    """GET a URL and parse the JSON response. Returns None on failure."""
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  HTTP request failed: {e}")
        return None


def post_json(
    url: str,
    data: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> dict[str, Any] | None:
    """POST JSON data and parse the JSON response. Returns None on failure."""
    hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)

    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  HTTP request failed: {e}")
        return None


def build_url(base: str, endpoint: str, params: dict[str, str] | None = None) -> str:
    """Build a URL with query parameters."""
    url = f"{base}{endpoint}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    return url
