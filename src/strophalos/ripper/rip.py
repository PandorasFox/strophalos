"""Title ripping — makemkvcon wrapper with progress monitoring and stall detection."""

from __future__ import annotations

import os
import signal
import subprocess
import time

# How long makemkvcon can go without producing any output before we kill it
STALL_TIMEOUT = 1800  # 30 minutes


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
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

        last_activity = time.monotonic()
        last_progress_time = last_activity
        last_pct = -1
        output_tail: list[str] = []
        timed_out = False

        assert proc.stdout is not None
        # Set stdout to non-blocking so we can check stall timeout
        import selectors

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)

        try:
            while proc.poll() is None:
                events = sel.select(timeout=10)
                if events:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    line = line.strip()
                    last_activity = time.monotonic()
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
                    # No output — check stall
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
                for sl in proc.stderr.read().strip().split("\n")[-5:]:
                    if sl:
                        print(f"  > {sl}")
