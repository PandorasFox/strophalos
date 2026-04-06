"""CLI entry point for rip-video — smart video disc ripper.

Scans disc, classifies content (movie/TV/music), rips selected titles,
and outputs STROPHALOS_* metadata lines for auto-rip.sh to parse.
"""

from __future__ import annotations

import argparse
import json
import os

from strophalos.ripper.classify import classify_disc
from strophalos.ripper.disc_id import compute_disc_id
from strophalos.ripper.rip import rip_titles
from strophalos.ripper.scan import (
    detect_media_type,
    get_title_chapters,
    get_title_durations,
    get_title_sizes,
    scan_disc,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart video disc ripper")
    parser.add_argument("--drive", type=int, default=0, help="MakeMKV drive ID")
    parser.add_argument("--output", default="/media/archive", help="Archive base directory")
    parser.add_argument("--label", default=None, help="Disc volume label (DRV_LABEL) for directory naming")
    parser.add_argument("--dry-run", action="store_true", help="Scan and classify only")
    args = parser.parse_args()

    print(f"Scanning disc in drive {args.drive}...")
    disc_label, titles, disc_info = scan_disc(args.drive)
    print(f"Disc label: {disc_label}")
    print(f"Found {len(titles)} title(s)")

    durations = get_title_durations(titles)
    chapters = get_title_chapters(titles)
    sizes = get_title_sizes(titles)

    print("\nTitle list:")
    for tid in sorted(durations.keys()):
        dur = durations[tid]
        ch = chapters.get(tid, "?")
        sz = sizes.get(tid, 0)
        mins, secs = divmod(dur, 60)
        hours, mins = divmod(mins, 60)
        sz_gb = sz / (1024**3) if sz else 0
        size_str = f", {sz_gb:.1f} GB" if sz else ""
        print(f"  Title {tid:2d}: {hours}:{mins:02d}:{secs:02d}  ({ch} chapters{size_str})")

    disc_type, to_rip, reason = classify_disc(durations, chapters, disc_label)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    media_type = detect_media_type(disc_info)
    print(f"Media type: {media_type}")

    # Compute disc ID before ripping (disc is still in drive)
    device = os.environ.get("DEVICE", "/dev/sr1")
    disc_id = compute_disc_id(media_type, device)

    if args.dry_run:
        print("\n[dry-run] Would rip the above titles.")
        return

    content_type = {"tv": "tv", "music": "music"}.get(disc_type, "movies")
    dir_label = args.label or disc_label or "unknown_disc"
    label_dir = os.path.join(args.output, content_type, "rips", media_type, dir_label)

    # Auto-increment disc number
    disc_num = 1
    while os.path.exists(os.path.join(label_dir, f"disc{disc_num}")):
        disc_num += 1
    out_dir = os.path.join(label_dir, f"disc{disc_num}")

    print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
    rip_titles(args.drive, to_rip, out_dir)

    # Persist disc ID in output directory
    if disc_id:
        disc_meta = {
            "disc_id": disc_id,
            "media_type": media_type,
            "disc_label": dir_label,
            "disc_number": disc_num,
            "id_type": "dvd_crc64" if media_type == "dvd" else "bd_sha256",
        }
        meta_path = os.path.join(out_dir, ".disc-id.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(disc_meta, f, indent=2)
        except Exception:
            pass

    # STROPHALOS_* output protocol — auto-rip.sh parses these
    print(f"STROPHALOS_OUTPUT_DIR={out_dir}")
    print(f"STROPHALOS_DISC_TYPE={disc_type}")
    print(f"STROPHALOS_MEDIA_TYPE={media_type}")
    print(f"STROPHALOS_TITLE_COUNT={len(to_rip)}")
    if disc_id:
        print(f"STROPHALOS_DISC_ID={disc_id}")
    print("Done.")


if __name__ == "__main__":
    main()
