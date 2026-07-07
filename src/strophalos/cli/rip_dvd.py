"""CLI entry point for rip-dvd — DVD ripping via dvdbackup + mkvmerge.

No SCSI, no makemkvcon. Scans via lsdvd, classifies titles, rips selected
ones through libdvdread/libdvdcss block reads.

Two-pass flow like rip-video: `plan_dvd_disc` scans/classifies and writes
the rip plan (first insertion); `rip_planned_dvd_disc` rips per an
existing plan (re-insertion).  `rip_dvd_disc` dispatches between them by
disc_id, with `single_pass=True` for the legacy one-insertion behavior.

Outputs STROPHALOS_* metadata lines for backward compatibility.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass

from strophalos.ripper.classify import classify_disc
from strophalos.ripper.disc_id import compute_dvd_disc_id
from strophalos.ripper.dvd import rip_dvd_titles, scan_dvd
from strophalos.ripper.orchestrate import run_rip, validate_rip
from strophalos.ripper.plan import (
    ClassificationRecord,
    PlanBlock,
    PlanValidationError,
    RipPlan,
    build_title_records,
    find_plan_by_disc_id,
    locate_plan,
    plan_path,
    validate_plan,
)
from strophalos.ripper.planner import (
    resolve_output_dir,
    seed_or_refresh_plan,
    wipe_rip_dir,
)
from strophalos.ripper.result import RipResult


@dataclass
class DvdScan:
    """Result of the lsdvd scan + classification for one DVD."""

    disc_label: str | None
    dir_label: str
    durations: dict[int, int]
    chapters: dict[int, int]
    disc_type: str
    to_rip: list[int]
    reason: str


def scan_dvd_disc(device: str, label: str | None = None) -> DvdScan | None:
    """Scan via lsdvd and classify.  Returns None when no titles found."""
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

    disc_type, to_rip, reason, _metadata = classify_disc(durations, chapters, dir_label)
    print(f"\nClassification: {disc_type}")
    print(f"Reason: {reason}")
    print(f"Titles to rip: {to_rip}")

    return DvdScan(
        disc_label=disc_label,
        dir_label=dir_label,
        durations=durations,
        chapters=chapters,
        disc_type=disc_type,
        to_rip=list(to_rip),
        reason=reason,
    )


def _write_plan_for_scan(scan: DvdScan, disc_id: str, out_dir: str, existing_plan: RipPlan | None) -> RipPlan:
    return seed_or_refresh_plan(
        out_dir=out_dir,
        disc_id=disc_id,
        id_type="dvd_crc64",
        disc_label=scan.disc_label,
        media_type="dvd",
        title_records=build_title_records(
            durations=scan.durations,
            chapters=scan.chapters,
            sizes={},
            source_filenames={},
            segment_maps={},
        ),
        classification=ClassificationRecord(
            disc_type=scan.disc_type,
            reason=scan.reason,
            suggested_titles_to_rip=list(scan.to_rip),
        ),
        existing=existing_plan,
        default_block=PlanBlock(disc_type=scan.disc_type, titles_to_rip=list(scan.to_rip)),
    )


def _execute_rip(device: str, out_dir: str, to_rip: list[int], wipe_existing: bool) -> int | None:
    if wipe_existing:
        wipe_rip_dir(out_dir)
    print(f"\nRipping {len(to_rip)} title(s) to {out_dir}...")
    if not run_rip(lambda: rip_dvd_titles(device, to_rip, out_dir), out_dir):
        return None
    valid = validate_rip(out_dir)
    if valid is None:
        return None
    print(f"Ripped {valid}/{len(to_rip)} title(s)")
    return valid


def _write_disc_id_meta(out_dir: str, disc_id: str | None, dir_label: str, disc_num: int) -> None:
    if not disc_id:
        return
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


def plan_dvd_disc(
    device: str,
    label: str | None = None,
    output: str = "/media/archive",
    disc_id: str | None = None,
) -> RipResult | None:
    """Pass 1: scan, classify, and write the rip plan.  No rip.

    Returns a `planned=True` RipResult, or None when the disc has no titles
    or can't be fingerprinted (no plan written — caller falls back
    single-pass).
    """
    scan = scan_dvd_disc(device, label)
    if scan is None:
        return None

    if disc_id is None:
        disc_id = compute_dvd_disc_id(device)
    if not disc_id:
        print("  Plan: skipped (no disc_id)")
        return None

    existing = find_plan_by_disc_id(output, disc_id)
    if existing:
        out_dir = str(existing[0])
        existing_plan = existing[1]
        print(f"\nRefreshing existing plan at {out_dir} (plan/identify blocks preserved)")
    else:
        existing_plan = None
        out_dir, _ = resolve_output_dir(
            disc_type=scan.disc_type,
            media_type="dvd",
            dir_label=scan.dir_label,
            disc_label=scan.disc_label,
            label=label,
            output=output,
        )

    plan = _write_plan_for_scan(scan, disc_id, out_dir, existing_plan)
    print(
        f"\nPlanned {len(plan.plan.titles_to_rip)} title(s).  Edit {plan_path(out_dir)} then re-insert to rip:\n"
        "    - plan.titles_to_rip / plan.disc_type — drives the rip.\n"
        "    - identify.url — whole-disc TMDb pin; identify.matches — per-title pins.\n"
        "    - rm the file to reset to classifier defaults."
    )
    return RipResult(
        output_dir=out_dir,
        disc_type=plan.plan.disc_type,
        media_type="dvd",
        title_count=len(plan.plan.titles_to_rip),
        disc_id=disc_id,
        label=scan.dir_label,
        planned=True,
    )


def rip_planned_dvd_disc(
    device: str,
    plan_dir: str,
    plan: RipPlan,
    label: str | None = None,
) -> RipResult | None:
    """Pass 2: rip a re-inserted DVD per its existing plan.

    Validates the plan against the scanned titles BEFORE wiping anything.
    Raises PlanValidationError when the plan no longer matches the disc.
    """
    scan = scan_dvd_disc(device, label)
    if scan is None:
        return None

    validate_plan(plan, scanned_tids=set(scan.durations))

    out_dir = str(plan_dir)
    to_rip = list(plan.plan.titles_to_rip)
    tag = " [user-edited]" if to_rip != list(plan.classification.suggested_titles_to_rip) else ""
    print(f"\nRipping per plan: {to_rip} (disc_type={plan.plan.disc_type})  [from {out_dir}]{tag}")

    plan = _write_plan_for_scan(scan, plan.disc_id, out_dir, plan)

    valid = _execute_rip(device, out_dir, to_rip, wipe_existing=True)
    if valid is None:
        return None

    _write_disc_id_meta(out_dir, plan.disc_id, scan.dir_label, disc_num=1)

    return RipResult(
        output_dir=out_dir,
        disc_type=plan.plan.disc_type,
        media_type="dvd",
        title_count=valid,
        disc_id=plan.disc_id,
        label=scan.dir_label,
    )


def _rip_single_pass(
    device: str,
    label: str | None,
    output: str,
    disc_id: str | None = None,
) -> RipResult | None:
    """Legacy one-insertion flow: scan, plan, and rip immediately.

    Still honors an existing plan; also the fallback when no disc_id can be
    computed (plan skipped)."""
    scan = scan_dvd_disc(device, label)
    if scan is None:
        return None

    if disc_id is None:
        disc_id = compute_dvd_disc_id(device)

    disc_type, to_rip = scan.disc_type, list(scan.to_rip)

    existing = find_plan_by_disc_id(output, disc_id) if disc_id else None
    if existing:
        out_dir = str(existing[0])
        disc_num = 1
        prev_plan = existing[1]
        disc_type = prev_plan.plan.disc_type
        to_rip = list(prev_plan.plan.titles_to_rip)
        tag = " [user-edited]" if to_rip != list(prev_plan.classification.suggested_titles_to_rip) else ""
        print(f"\nUsing existing plan: {to_rip} (disc_type={disc_type})  [from {out_dir}]{tag}")
    else:
        prev_plan = None
        out_dir, disc_num = resolve_output_dir(
            disc_type=disc_type,
            media_type="dvd",
            dir_label=scan.dir_label,
            disc_label=scan.disc_label,
            label=label,
            output=output,
        )

    if disc_id:
        _write_plan_for_scan(scan, disc_id, out_dir, prev_plan)
    else:
        print("  Plan: skipped (no disc_id)")

    valid = _execute_rip(device, out_dir, to_rip, wipe_existing=existing is not None)
    if valid is None:
        return None

    _write_disc_id_meta(out_dir, disc_id, scan.dir_label, disc_num)

    return RipResult(
        output_dir=out_dir,
        disc_type=disc_type,
        media_type="dvd",
        title_count=valid,
        disc_id=disc_id,
        label=scan.dir_label,
    )


def rip_dvd_disc(
    device: str,
    label: str | None = None,
    output: str = "/media/archive",
    single_pass: bool = False,
) -> RipResult | None:
    """One-call dispatch (rip-dvd CLI and single-pass fallback).

    Two-pass by default: no plan for this disc → write one and return a
    `planned=True` result (caller ejects for review); plan exists → validate
    and rip per plan.  Raises PlanValidationError when an existing plan
    fails validation.
    """
    disc_id = compute_dvd_disc_id(device)

    if single_pass or not disc_id:
        if not disc_id:
            print("no disc_id (mount failed?) — falling back to single-pass rip")
        return _rip_single_pass(device, label, output, disc_id)

    hit = locate_plan(output, disc_id)
    if hit is not None and hit.plan is None:
        raise PlanValidationError(f"{hit.error}\nFix the JSON (or rm it) and retry.")
    if hit is None:
        return plan_dvd_disc(device, label, output, disc_id=disc_id)

    validate_plan(hit.plan)
    return rip_planned_dvd_disc(device, str(hit.rip_dir), hit.plan, label=label)


def main() -> None:
    parser = argparse.ArgumentParser(description="DVD ripper (dvdbackup + mkvmerge)")
    parser.add_argument("--device", default=None, help="DVD device (default: $DEVICE or /dev/sr1)")
    parser.add_argument("--output", default="/media/archive", help="Archive base directory")
    parser.add_argument("--label", default=None, help="Disc volume label for directory naming")
    parser.add_argument(
        "--plan-only",
        "--dry-run",
        action="store_true",
        dest="plan_only",
        help="Scan/classify and write the rip plan only (no rip)",
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="Legacy one-insertion flow: scan and rip immediately (no plan review stop)",
    )
    args = parser.parse_args()

    device = args.device or os.environ.get("DEVICE", "/dev/sr1")

    if args.plan_only:
        result = plan_dvd_disc(device, args.label, args.output)
    else:
        try:
            result = rip_dvd_disc(device, args.label, args.output, single_pass=args.single_pass)
        except PlanValidationError as e:
            print(f"rip plan invalid:\n{e}")
            result = None

    if result and not result.planned and not args.plan_only:
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
