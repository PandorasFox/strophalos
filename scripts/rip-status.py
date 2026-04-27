#!/usr/bin/env python3
"""Rip status — parse the strophalos container log + filesystem to summarize the active rip.

Designed for `watch -n30 scripts/rip-status.py`. Prints a short status block when
a rip is in progress, or a one-liner when idle.

Overrides via env:
  STROPHALOS_CONTAINER  container name (default: strophalos)
  STROPHALOS_MEDIA      host path that maps to /media in the container
                        (default: /mnt/cerberus/library)
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys

CONTAINER = os.environ.get("STROPHALOS_CONTAINER", "strophalos")
HOST_MEDIA = os.environ.get("STROPHALOS_MEDIA", "/mnt/cerberus/library")
CONTAINER_MEDIA = "/media"


def get_logs(n: int = 5000) -> list[tuple[dt.datetime, str]]:
    res = subprocess.run(
        ["docker", "logs", "-t", "--tail", str(n), CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[tuple[dt.datetime, str]] = []
    for ln in (res.stdout + res.stderr).splitlines():
        m = re.match(r"(\S+)\s(.*)$", ln)
        if not m:
            continue
        try:
            ts = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append((ts, m.group(2)))
    return out


def host_path(container_path: str) -> str:
    if container_path.startswith(CONTAINER_MEDIA + "/"):
        return HOST_MEDIA + container_path[len(CONTAINER_MEDIA) :]
    return container_path


def fmt_gib(b: int) -> str:
    return f"{b / 1024**3:.2f} GiB"


def fmt_dur(secs: float) -> str:
    s = int(secs)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def main() -> int:
    now = dt.datetime.now(dt.UTC)
    header = f"Strophalos rip status — {now.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
    print(header)
    print()

    try:
        logs = get_logs()
    except FileNotFoundError:
        print("  docker not on PATH", file=sys.stderr)
        return 1
    if not logs:
        print(f"  no logs from container '{CONTAINER}' (is it running?)")
        return 0

    # Most recent "Ripping title N (i/n)..." is the active title (if not yet terminated).
    rip_idx = None
    rip_match = None
    for i in range(len(logs) - 1, -1, -1):
        m = re.match(r"\s*(?:\[\d\d:\d\d:\d\d\]\s+)?Ripping title (\d+) \((\d+)/(\d+)\)\.\.\.", logs[i][1])
        if m:
            rip_idx = i
            rip_match = m
            break

    if rip_idx is None:
        print("  no rip found in recent log history")
        return 0

    # If a disc_rip_terminated hook ran after the rip start, the rip is done.
    post = logs[rip_idx + 1 :]
    terminated_ts = next(
        (ts for ts, ln in post if "hook: disc_rip_terminated.sh" in ln or ln.startswith("disc_rip_terminated:")),
        None,
    )
    if terminated_ts is not None:
        ago = fmt_dur((now - terminated_ts).total_seconds())
        # Pull the last disc label for context.
        last_label = None
        for _ts, ln in reversed(post):
            m = re.match(r"disc_rip_terminated:\s*label='([^']*)'", ln)
            if m:
                last_label = m.group(1)
                break
        label_str = f" — last rip: {last_label}" if last_label else ""
        print(f"  no active rip (last terminated {ago} ago{label_str})")
        return 0

    title_id = int(rip_match.group(1))
    cur_i, cur_n = int(rip_match.group(2)), int(rip_match.group(3))
    start_ts = logs[rip_idx][0]

    # Walk backward for the output dir, disc label, and title size from the scan block.
    # Stop once we reach the current disc's "Disc label:" line — anything before
    # that belongs to a prior rip and would leak wrong sizes (e.g. a BD's title-3
    # size into a DVD rip whose scan block doesn't print sizes).
    output_dir = None
    disc_label = None
    expected_bytes = None
    for j in range(rip_idx, -1, -1):
        ln = logs[j][1]
        if output_dir is None:
            m = re.match(r"\s*Ripping \d+ title\(s\) to (.+?)\.\.\.$", ln)
            if m:
                output_dir = m.group(1).strip()
        if expected_bytes is None:
            m = re.match(
                rf"\s*Title\s+0*{title_id}:\s+\S+\s+\(\S+\s+chapters,\s+([\d.]+)\s+GB\)",
                ln,
            )
            if m:
                # rip_video.py divides size by 1024**3 before printing — value is GiB.
                expected_bytes = int(float(m.group(1)) * 1024**3)
        m = re.match(r"Disc label:\s*(.+)$", ln)
        if m:
            disc_label = m.group(1).strip()
            break

    # Locate the mkv for this title on disk.
    mkv_path = None
    cur_bytes = 0
    if output_dir:
        hpath = host_path(output_dir)
        if os.path.isdir(hpath):
            mkvs = [f for f in os.listdir(hpath) if f.endswith(".mkv")]
            match = [f for f in mkvs if re.search(rf"_t0*{title_id}\.mkv$", f)]
            if match:
                mkv_path = os.path.join(hpath, match[0])
            elif mkvs:
                mkvs.sort(key=lambda f: os.path.getmtime(os.path.join(hpath, f)), reverse=True)
                mkv_path = os.path.join(hpath, mkvs[0])
            if mkv_path:
                try:
                    cur_bytes = os.path.getsize(mkv_path)
                except OSError:
                    pass

    elapsed = max(0.0, (now - start_ts).total_seconds())

    print(f"  Disc:    {disc_label or '?'}")
    print(f"  Title:   {title_id}  ({cur_i}/{cur_n} in this rip)")
    if mkv_path:
        print(f"  File:    {mkv_path}")
    elif output_dir:
        print(f"  Dir:     {host_path(output_dir)} (no mkv yet)")
    if expected_bytes:
        pct = 100.0 * cur_bytes / expected_bytes if expected_bytes else 0.0
        print(f"  Size:    {fmt_gib(cur_bytes)} / {fmt_gib(expected_bytes)}  ({pct:.1f}%)")
    else:
        print(f"  Size:    {fmt_gib(cur_bytes)}  (no scan size found)")
    print(f"  Elapsed: {fmt_dur(elapsed)}")
    if elapsed > 0 and cur_bytes > 0:
        rate = cur_bytes / elapsed
        print(f"  Rate:    {rate / 1024**2:.1f} MiB/s  ({rate * 60 / 1024**3:.2f} GiB/min)")
        if expected_bytes and cur_bytes < expected_bytes:
            remaining = (expected_bytes - cur_bytes) / rate
            finish = now + dt.timedelta(seconds=remaining)
            print(f"  ETA:     ~{fmt_dur(remaining)}  (finish ~{finish.astimezone().strftime('%H:%M:%S')})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
