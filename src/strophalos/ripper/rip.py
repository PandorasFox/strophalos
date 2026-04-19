"""Title ripping — makemkvcon wrapper with progress monitoring and stall detection."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time

# How long makemkvcon can go without producing any output before we kill it.
# makemkvcon appears to block-buffer stdout on non-TTY during the `mkv` command,
# so in practice this fires after N seconds of total rip time, not actual
# silence. Kept generous as a safety net for truly wedged processes.
STALL_TIMEOUT = 10800  # 3 hours


def rip_titles(
    drive_id: int,
    title_ids: list[int],
    output_dir: str,
    min_length: int | None = None,
) -> None:
    """Rip specific titles from disc via makemkvcon."""
    os.makedirs(output_dir, exist_ok=True)

    opts = ["-r"]
    if min_length:
        opts.append(f"--minlength={min_length}")

    PROGRESS_INTERVAL = 180  # seconds between progress log lines

    for i, tid in enumerate(title_ids):
        print(f"  Ripping title {tid} ({i + 1}/{len(title_ids)})...", flush=True)
        cmd = ["makemkvcon"] + opts + ["mkv", f"disc:{drive_id}", str(tid), output_dir]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        last_activity = time.monotonic()
        last_progress_time = last_activity
        last_pct = -1
        output_tail: list[str] = []
        timed_out = False
        read_buf = b""

        assert proc.stdout is not None
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)

        try:
            while proc.poll() is None:
                events = sel.select(timeout=10)
                if events:
                    chunk = proc.stdout.read1(4096)
                    if not chunk:
                        break
                    # Reset stall on any wire activity, even partial lines (e.g.
                    # progress that arrives as \r-updated output).
                    last_activity = time.monotonic()
                    read_buf += chunk

                    while True:
                        idx = -1
                        for sep in (b"\n", b"\r"):
                            pos = read_buf.find(sep)
                            if pos != -1 and (idx == -1 or pos < idx):
                                idx = pos
                        if idx == -1:
                            break
                        raw = read_buf[:idx]
                        read_buf = read_buf[idx + 1 :]
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        output_tail.append(line)
                        if len(output_tail) > 20:
                            output_tail.pop(0)

                        if line.startswith("PRGV:"):
                            parts = line[5:].split(",")
                            if len(parts) >= 3:
                                try:
                                    current, _total, pmax = int(parts[0]), int(parts[1]), int(parts[2])
                                    pct = int(current * 100 / pmax) if pmax > 0 else 0
                                    now = time.monotonic()
                                    if pct != last_pct and (now - last_progress_time) >= PROGRESS_INTERVAL:
                                        print(f"  title {tid}: {pct}% ({i + 1}/{len(title_ids)})", flush=True)
                                        last_progress_time = now
                                        last_pct = pct
                                except (ValueError, ZeroDivisionError):
                                    pass
                else:
                    if time.monotonic() - last_activity > STALL_TIMEOUT:
                        print(f"  title {tid}: no output for {STALL_TIMEOUT}s, killing", flush=True)
                        timed_out = True
                        os.killpg(proc.pid, signal.SIGKILL)
                        break
        finally:
            sel.unregister(proc.stdout)
            sel.close()

        proc.wait()
        if timed_out:
            print(f"  WARNING: title {tid} killed (stalled)")
        elif proc.returncode != 0:
            print(f"  WARNING: title {tid} failed (rc={proc.returncode})")
            for sl in output_tail[-5:]:
                print(f"  > {sl}")
            if proc.stderr:
                stderr_text = proc.stderr.read().decode("utf-8", errors="replace")
                for sl in stderr_text.strip().split("\n")[-5:]:
                    if sl:
                        print(f"  > {sl}")
