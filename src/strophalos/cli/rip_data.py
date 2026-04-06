"""CLI entry point for rip-data — rip data/game discs to ISO."""

from __future__ import annotations

import argparse
import os
import subprocess

from strophalos.core.notify import notify


def _get_disc_size(device: str) -> int:
    """Get disc size in bytes via blockdev."""
    try:
        result = subprocess.run(
            ["blockdev", "--getsize64", device],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return int(result.stdout.strip())
    except Exception:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Rip data/game disc to ISO")
    parser.add_argument("--device", default=None, help="Disc device (default: $DEVICE or /dev/sr1)")
    parser.add_argument("--output", default="/media/archive/iso", help="Output directory for ISOs")
    parser.add_argument("--label", default=None, help="Disc label for filename")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be ripped")
    args = parser.parse_args()

    device = args.device or os.environ.get("DEVICE", "/dev/sr1")
    label = args.label or "unknown_disc"
    safe_label = label.replace("/", "_").replace(" ", "_")

    disc_size = _get_disc_size(device)
    size_mb = disc_size / (1024**2) if disc_size else 0
    print(f"Data disc: {label} ({size_mb:.0f} MB)")

    out_dir = args.output
    os.makedirs(out_dir, exist_ok=True)

    # Avoid overwriting existing ISOs
    out_path = os.path.join(out_dir, f"{safe_label}.iso")
    counter = 1
    while os.path.exists(out_path):
        counter += 1
        out_path = os.path.join(out_dir, f"{safe_label}_{counter}.iso")

    if args.dry_run:
        print(f"[dry-run] Would rip to {out_path}")
        return

    print(f"Ripping to {out_path}...")
    notify("Ripping data disc", f"{label} ({size_mb:.0f} MB)")

    result = subprocess.run(
        ["dd", f"if={device}", f"of={out_path}", "bs=2048", "status=progress"],
        timeout=7200,
    )

    if result.returncode != 0:
        print(f"dd failed (rc={result.returncode})")
        notify("Data disc rip failed", f"{label} (exit {result.returncode})", error=True)
        # Clean up partial ISO
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return

    final_size = os.path.getsize(out_path) / (1024**2)
    print(f"Done: {out_path} ({final_size:.0f} MB)")
    notify("Data disc ripped", f"{label}\n{final_size:.0f} MB → {out_path}")

    # STROPHALOS_* output protocol
    print(f"STROPHALOS_OUTPUT_DIR={out_dir}")
    print("STROPHALOS_DISC_TYPE=data")
    print("STROPHALOS_MEDIA_TYPE=data")
    print("STROPHALOS_TITLE_COUNT=1")
    print("Done.")


if __name__ == "__main__":
    main()
