"""rip-plan — dump, show, or validate the per-disc rip plan without ripping.

The daemon writes a plan automatically on a disc's first insertion and
ejects; edit the plan while the disc is out, re-insert, and it rips per
the plan.  This CLI covers the manual sides of that loop (run via
`docker exec` / `docker compose run`):

  - `rip-plan --drive 0` — scan the disc in the drive and write/refresh
    `.rip-plan.json` (same as the daemon's first pass; no rip).
  - Edit the plan: `plan.titles_to_rip` / `plan.disc_type` drive the rip;
    `identify.url` pins the whole disc to a TMDb/MusicBrainz URL;
    `identify.matches` maps individual titles to entries by URL
    (multi-movie discs).  `rm` the file to reset to classifier defaults.
  - `rip-plan --validate DISC_ID_OR_PATH` — check an edited plan (URL
    parsing + consistency) before re-inserting the disc.
  - `rip-plan --show DISC_ID_OR_PATH` — pretty-print an existing plan
    without touching the drive.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from strophalos.cli.rip_video import plan_video_disc
from strophalos.ripper.plan import (
    PlanValidationError,
    find_plan_by_disc_id,
    parse_provider_url,
    plan_path,
    read_plan,
    validate_plan,
)


def _resolve_plan_file(target: str, archive_root: str, bd_audio_root: str) -> Path | None:
    """Resolve a disc_id or file/dir path to a plan file path."""
    p = Path(target)
    if p.is_dir():
        p = plan_path(p)
    if p.exists():
        return p
    hit = find_plan_by_disc_id([archive_root, bd_audio_root], target)
    if hit is None:
        return None
    return plan_path(hit[0])


def _validate_plan_file(target: str, archive_root: str, bd_audio_root: str) -> int:
    """Validate an edited plan before re-inserting the disc.  Exit 0/1."""
    p = _resolve_plan_file(target, archive_root, bd_audio_root)
    if p is None:
        print(f"no plan found for disc_id/path: {target}", file=sys.stderr)
        return 1

    try:
        json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"INVALID {p}: not parseable JSON ({e})", file=sys.stderr)
        print("note: a syntactically broken plan can't be matched by disc_id on re-insert", file=sys.stderr)
        return 1

    plan = read_plan(p.parent)
    if plan is None:
        print(f"INVALID {p}: JSON parses but doesn't fit the plan schema", file=sys.stderr)
        return 1

    try:
        validate_plan(plan)
    except PlanValidationError as e:
        print(f"INVALID {p}:", file=sys.stderr)
        for line in str(e).splitlines():
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"OK {p}")
    print(f"  {plan.plan.disc_type} — titles_to_rip {plan.plan.titles_to_rip}")
    if plan.identify.url:
        print(f"  whole-disc pin: {plan.identify.url}")
    for m in plan.identify.matches:
        titles = m.titles or "whole disc"
        print(f"  match: {m.url} → titles {titles}")
    return 0


def _show_plan(target: str, archive_root: str, bd_audio_root: str) -> int:
    """Pretty-print an existing plan.  `target` is a disc_id or a file/dir path."""
    p = _resolve_plan_file(target, archive_root, bd_audio_root)
    if p is None:
        print(f"no plan found for disc_id/path: {target}", file=sys.stderr)
        return 1

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
    if pl.get("titles_to_rip") != cls.get("suggested_titles_to_rip"):
        flags.append("USER-EDITED")
    if pl.get("disc_type") and pl.get("disc_type") != cls.get("disc_type"):
        flags.append(f"TYPE-OVERRIDDEN ({cls.get('disc_type')} → {pl.get('disc_type')})")
    flag = f" [{', '.join(flags)}]" if flags else ""
    print(f"  plan:       {pl.get('disc_type')} — titles_to_rip {pl.get('titles_to_rip')}{flag}")
    if pl.get("note"):
        print(f"              note: {pl['note']}")
    ident = data.get("identify") or {}
    if ident.get("tmdb_id"):
        season = f" season={ident['season']}" if ident.get("season") is not None else ""
        ident_note = f"  ({ident['note']})" if ident.get("note") else ""
        print(f"  identify:   tmdb_id={ident['tmdb_id']} type={ident.get('tmdb_type')}{season}{ident_note}")
    if ident.get("url"):
        print(f"  identify:   url={ident['url']}{_parsed_suffix(ident['url'])}")
    for m in ident.get("matches") or []:
        titles = m.get("titles") or "whole disc"
        m_note = f"  ({m['note']})" if m.get("note") else ""
        print(f"  match:      {m.get('url')}{_parsed_suffix(m.get('url', ''))} → titles {titles}{m_note}")
    print()
    print("  Titles:")
    for t in data.get("titles", []):
        drop = f" [DROPPED: {t['dropped']}]" if t.get("dropped") else ""
        sz = f", {t['size_gb']:.1f} GB" if t.get("size_gb") else ""
        seg = f"  segs={t['segment_map']}" if t.get("segment_map") else ""
        src = f"  src={t['source_filename']}" if t.get("source_filename") else ""
        print(f"    Title {t['tid']:2d}: {t['duration']}  ({t.get('chapters', '?')} chapters{sz}){src}{seg}{drop}")
    return 0


def _parsed_suffix(url: str) -> str:
    """' [tmdb movie 603]'-style annotation for a provider URL, or ' [UNPARSEABLE]'."""
    try:
        p = parse_provider_url(url)
    except PlanValidationError:
        return "  [UNPARSEABLE]"
    season = f" season {p.season}" if p.season is not None else ""
    return f"  [{p.provider} {p.kind} {p.id}{season}]"


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
    parser.add_argument(
        "--validate",
        metavar="DISC_ID_OR_PATH",
        help="Validate an edited plan (URLs + consistency) before re-inserting the disc",
    )
    args = parser.parse_args()

    if args.validate:
        return _validate_plan_file(args.validate, args.output, args.output_bd_audio)

    if args.show:
        return _show_plan(args.show, args.output, args.output_bd_audio)

    result = plan_video_disc(args.drive, args.label, args.output, args.output_bd_audio)
    if result is None:
        print("scan/classify failed or no disc_id computed — plan was NOT written", file=sys.stderr)
        return 1
    print(f"\nPlan written: {plan_path(result.output_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
