"""rip-plan — dump (or show) the per-disc rip plan without ripping.

Designed to be run via `docker exec` / `docker compose run` against a
stopped or idle container:

  1. `rip-plan --drive 0` — scan the disc, write `.rip-plan.json` into the
     prospective rip output directory (classifier's choice), print the
     path.  No rip happens.
  2. Edit the plan file (set `manual_override: true`, adjust
     `titles_to_rip` / `disc_type`).
  3. Reinsert the disc and run the normal rip — the override wins.

`rip-plan --show DISC_ID` walks the archive for an existing plan and
pretty-prints it without touching the drive.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from strophalos.cli.rip_video import rip_video_disc
from strophalos.ripper.plan import find_plan_by_disc_id, plan_path


def _show_plan(target: str, archive_root: str, bd_audio_root: str) -> int:
    """Pretty-print an existing plan.  `target` is a disc_id or a file/dir path."""
    p = Path(target)
    if p.is_dir():
        p = plan_path(p)
    if not p.exists():
        hit = find_plan_by_disc_id([archive_root, bd_audio_root], target)
        if hit is None:
            print(f"no plan found for disc_id/path: {target}", file=sys.stderr)
            return 1
        p = plan_path(hit[0])

    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"failed to read plan {p}: {e}", file=sys.stderr)
        return 1

    print(f"Plan: {p}")
    print(f"  disc_id:    {data.get('disc_id')}")
    print(f"  disc_label: {data.get('disc_label')}")
    print(f"  media_type: {data.get('media_type')}")
    print(f"  scanned:    {data.get('scanned_at')}")
    cls = data.get("classification", {})
    print(f"  classifier: {cls.get('disc_type')} — suggested {cls.get('suggested_titles_to_rip')}")
    print(f"              ({cls.get('reason')})")
    pl = data.get("plan", {})
    flags = []
    if pl.get("manual_override"):
        flags.append("OVERRIDE")
    if pl.get("forced_rip_all"):
        flags.append("FORCED-RIP-ALL")
    flag = f" [{', '.join(flags)}]" if flags else ""
    print(f"  plan:       {pl.get('disc_type')} — titles_to_rip {pl.get('titles_to_rip')}{flag}")
    if pl.get("note"):
        print(f"              note: {pl['note']}")
    ident = data.get("identify") or {}
    if ident.get("tmdb_id"):
        season = f" season={ident['season']}" if ident.get("season") is not None else ""
        ident_note = f"  ({ident['note']})" if ident.get("note") else ""
        print(f"  identify:   tmdb_id={ident['tmdb_id']} type={ident.get('tmdb_type')}{season}{ident_note}")
    print()
    print("  Titles:")
    for t in data.get("titles", []):
        drop = f" [DROPPED: {t['dropped']}]" if t.get("dropped") else ""
        sz = f", {t['size_gb']:.1f} GB" if t.get("size_gb") else ""
        seg = f"  segs={t['segment_map']}" if t.get("segment_map") else ""
        src = f"  src={t['source_filename']}" if t.get("source_filename") else ""
        print(f"    Title {t['tid']:2d}: {t['duration']}  ({t.get('chapters', '?')} chapters{sz}){src}{seg}{drop}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan a disc and dump its rip plan without ripping. "
        "Edit the resulting JSON to override titles_to_rip on the next rip.",
    )
    parser.add_argument("--drive", type=int, default=0, help="MakeMKV drive ID")
    parser.add_argument(
        "--output",
        default="/media/archive",
        help="Archive base directory (plans live inside each rip's output directory)",
    )
    parser.add_argument("--label", default=None, help="Override disc volume label")
    parser.add_argument(
        "--output-bd-audio",
        default="/output-bd",
        help="Audio Blu-ray output base directory (also searched by --show)",
    )
    parser.add_argument(
        "--show",
        metavar="DISC_ID_OR_PATH",
        help="Pretty-print an existing plan (walks archive if given a disc_id)",
    )
    args = parser.parse_args()

    if args.show:
        return _show_plan(args.show, args.output, args.output_bd_audio)

    result = rip_video_disc(args.drive, args.label, args.output, dry_run=True)
    if result is None:
        print("scan/classify failed", file=sys.stderr)
        return 1
    if not result.disc_id:
        print("no disc_id computed (mount failed?) — plan was NOT written", file=sys.stderr)
        return 2
    print(f"\nPlan written: {plan_path(result.output_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
