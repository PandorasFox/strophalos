"""CLI entry point for probe-disc — detect disc type without SCSI/makemkvcon.

Outputs key=value lines for auto-rip.sh to parse:
  DISC_TYPE=audio|dvd|bluray|data|audio+data|unknown
  DISC_LABEL=...
  DISC_ID=...       (MusicBrainz disc ID, if audio tracks present)
  HAS_AUDIO=0|1
  HAS_DATA=0|1
"""

from __future__ import annotations

import argparse
import os

from strophalos.ripper.probe import probe_disc


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect disc type")
    parser.add_argument("--device", default=None, help="Disc device (default: $DEVICE or /dev/sr1)")
    args = parser.parse_args()

    device = args.device or os.environ.get("DEVICE", "/dev/sr1")
    result = probe_disc(device)

    print(f"DISC_TYPE={result.disc_type}")
    print(f"DISC_LABEL={result.label}")
    print(f"HAS_AUDIO={int(result.has_audio)}")
    print(f"HAS_DATA={int(result.has_data)}")
    if result.disc_id:
        print(f"DISC_ID={result.disc_id}")


if __name__ == "__main__":
    main()
