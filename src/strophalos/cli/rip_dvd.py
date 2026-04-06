"""CLI entry point for rip-dvd — DVD ripping via dvdbackup + mkvmerge.

No SCSI, no makemkvcon. Scans via lsdvd, classifies titles, rips selected
ones through libdvdread/libdvdcss block reads.

Outputs STROPHALOS_* metadata lines for auto-rip.sh to parse.
"""

from __future__ import annotations

import argparse
import json
import os

from strophalos.ripper.classify import classify_disc
from strophalos.ripper.disc_id import compute_dvd_disc_id
from strophalos.ripper.dvd import rip_dvd_titles, scan_dvd


def main() -> None:
    parser = argparse.ArgumentParser(description="DVD ripper (dvdbackup + mkvmerge)")
    parser.add_argument("--device", default=None, help="DVD device (default: $DEVICE or /dev/sr1)")
    parser.add_argument("--output", default="/media/archive", help="Archive base directory")
    parser.add_argument("--label", default=None, help="Disc volume label for directory naming")
    parser.add_argument("--dry-run", action="store_true", help="Scan and classify only")
    args = parser.parse_args()

    device = args.device or os.environ.get("DEVICE", "/dev/sr1")

    print(f"Scanning DVD on {device}...")
    disc_label, durations, chapters = scan_dvd(device)

    label = args.label or disc_label or "unknown_dvd"
    print(f"Disc label: {label}")
    print(f"Found {len(durations)} title(s)")

    if not durations:
        print("No titles found on disc")
        return

    print("\nTitle list:")
    for tid in sorted(durations.keys()):
        dur = durations[tid]
        ch = chapters.get(tid, 0)
        mins, secs = divmod(dur, 60)
        hours, mins = divmod(mins, 60)
        print(f"  Title {tid:2d}: {hours}:{mins:02d}:{secs:02d}  ({ch} chapters)")

    disc_type, to_rip, reason = classify_disc(durations, chapters, label)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    # Compute disc ID (mount + pydvdid CRC64)
    disc_id = compute_dvd_disc_id(device)

    if args.dry_run:
        print("\n[dry-run] Would rip the above titles.")
        return

    content_type = {"tv": "tv", "music": "music"}.get(disc_type, "movies")
    label_dir = os.path.join(args.output, content_type, "rips", "dvd", label)

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
            "disc_label": label,
            "disc_number": disc_num,
            "id_type": "dvd_crc64",
        }
        meta_path = os.path.join(out_dir, ".disc-id.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(disc_meta, f, indent=2)
        except Exception:
            pass

    # STROPHALOS_* output protocol
    print(f"STROPHALOS_OUTPUT_DIR={out_dir}")
    print(f"STROPHALOS_DISC_TYPE={disc_type}")
    print("STROPHALOS_MEDIA_TYPE=dvd")
    print(f"STROPHALOS_TITLE_COUNT={len(to_rip)}")
    if disc_id:
        print(f"STROPHALOS_DISC_ID={disc_id}")
    print("Done.")


if __name__ == "__main__":
    main()
