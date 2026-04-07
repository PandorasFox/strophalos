"""CLI entry point for rip-dvd — DVD ripping via dvdbackup + mkvmerge.

No SCSI, no makemkvcon. Scans via lsdvd, classifies titles, rips selected
ones through libdvdread/libdvdcss block reads.

Outputs STROPHALOS_* metadata lines for backward compatibility.
"""

from __future__ import annotations

import argparse
import json
import os

from strophalos.ripper.classify import classify_disc
from strophalos.ripper.disc_id import compute_dvd_disc_id
from strophalos.ripper.dvd import rip_dvd_titles, scan_dvd
from strophalos.ripper.result import RipResult


def rip_dvd_disc(
    device: str,
    label: str | None = None,
    output: str = "/media/archive",
    dry_run: bool = False,
) -> RipResult | None:
    """Core DVD rip logic. Returns structured result or None on failure."""
    print(f"Scanning DVD on {device}...")
    disc_label, durations, chapters = scan_dvd(device)

    dir_label = label or disc_label or "unknown_dvd"
    print(f"Disc label: {dir_label}")
    print(f"Found {len(durations)} title(s)")

    if not durations:
        print("No titles found on disc")
        return None

    print("\nTitle list:")
    for tid in sorted(durations.keys()):
        dur = durations[tid]
        ch = chapters.get(tid, 0)
        mins, secs = divmod(dur, 60)
        hours, mins = divmod(mins, 60)
        print(f"  Title {tid:2d}: {hours}:{mins:02d}:{secs:02d}  ({ch} chapters)")

    disc_type, to_rip, reason = classify_disc(durations, chapters, dir_label)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    # Compute disc ID (mount + pydvdid CRC64)
    disc_id = compute_dvd_disc_id(device)

    if dry_run:
        print("\n[dry-run] Would rip the above titles.")
        return RipResult(
            output_dir="",
            disc_type=disc_type,
            media_type="dvd",
            title_count=len(to_rip),
            disc_id=disc_id,
            label=dir_label,
        )

    content_type = {"tv": "tv", "music": "music"}.get(disc_type, "movies")
    label_dir = os.path.join(output, content_type, "rips", "dvd", dir_label)

    # Auto-increment disc number
    disc_num = 1
    while os.path.exists(os.path.join(label_dir, f"disc{disc_num}")):
        disc_num += 1
    out_dir = os.path.join(label_dir, f"disc{disc_num}")

    print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
    ripped = rip_dvd_titles(device, to_rip, out_dir)
    print(f"Ripped {len(ripped)}/{len(to_rip)} title(s)")

    # Persist disc ID
    if disc_id:
        disc_meta = {
            "disc_id": disc_id,
            "media_type": "dvd",
            "disc_label": dir_label,
            "disc_number": disc_num,
            "id_type": "dvd_crc64",
        }
        meta_path = os.path.join(out_dir, ".disc-id.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(disc_meta, f, indent=2)
        except Exception:
            pass

    return RipResult(
        output_dir=out_dir,
        disc_type=disc_type,
        media_type="dvd",
        title_count=len(to_rip),
        disc_id=disc_id,
        label=dir_label,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="DVD ripper (dvdbackup + mkvmerge)")
    parser.add_argument("--device", default=None, help="DVD device (default: $DEVICE or /dev/sr1)")
    parser.add_argument("--output", default="/media/archive", help="Archive base directory")
    parser.add_argument("--label", default=None, help="Disc volume label for directory naming")
    parser.add_argument("--dry-run", action="store_true", help="Scan and classify only")
    args = parser.parse_args()

    device = args.device or os.environ.get("DEVICE", "/dev/sr1")
    result = rip_dvd_disc(device, args.label, args.output, args.dry_run)

    if result and not args.dry_run:
        # STROPHALOS_* output protocol — backward compatibility
        print(f"STROPHALOS_OUTPUT_DIR={result.output_dir}")
        print(f"STROPHALOS_DISC_TYPE={result.disc_type}")
        print("STROPHALOS_MEDIA_TYPE=dvd")
        print(f"STROPHALOS_TITLE_COUNT={result.title_count}")
        if result.disc_id:
            print(f"STROPHALOS_DISC_ID={result.disc_id}")
    print("Done.")


if __name__ == "__main__":
    main()
