"""Shared planning stage for video-media rips (BD/UHD/DVD).

Consolidates the logic that was duplicated between rip_video and rip_dvd:
output-directory resolution, plan seeding/refreshing, and the cheap
early disc-id computation (mount + hash, no makemkvcon) that lets the
daemon recognize a re-inserted disc before any slow scan.
"""

from __future__ import annotations

import os

from strophalos.core.fs import parse_season_disc, sanitize_filename
from strophalos.ripper.disc_id import compute_bd_disc_id, compute_dvd_disc_id
from strophalos.ripper.plan import (
    ClassificationRecord,
    PlanBlock,
    RipPlan,
    TitleRecord,
    build_plan,
    materialize_match_fields,
    plan_path,
    write_plan,
)

# Identify-stage artifacts wiped alongside stale MKVs on a re-rip so the
# identify loop re-runs cleanly against the fresh files.
IDENTIFY_ARTIFACTS = (
    ".movie-manifest.json",
    ".episode-manifest.json",
    ".music-manifest.json",
    ".identify-state.json",
    ".identify-conflict.json",
)


def wipe_rip_dir(out_dir: str) -> None:
    """Remove stale MKVs and identify artifacts before a re-rip.

    Plan, rip-manifest and disc-id files are preserved.
    """
    if not os.path.isdir(out_dir):
        return
    for entry in os.scandir(out_dir):
        if entry.is_file() and (entry.name.endswith(".mkv") or entry.name in IDENTIFY_ARTIFACTS):
            os.unlink(entry.path)


def compute_early_disc_id(probe_disc_type: str, device: str) -> tuple[str | None, str | None]:
    """Compute the disc fingerprint before any makemkvcon scan.

    Cheap (read-only mount + hash of structural metadata).  Returns
    (disc_id, id_type) or (None, None) when the disc can't be mounted or
    fingerprinted — callers fall back to the single-pass flow.
    """
    if probe_disc_type == "dvd":
        disc_id = compute_dvd_disc_id(device)
        return (disc_id, "dvd_crc64") if disc_id else (None, None)
    # bluray or unknown — try BD structure first, then DVD for unknowns
    disc_id = compute_bd_disc_id(device)
    if disc_id:
        return disc_id, "bd_sha256"
    if probe_disc_type == "unknown":
        disc_id = compute_dvd_disc_id(device)
        if disc_id:
            return disc_id, "dvd_crc64"
    return None, None


def resolve_output_dir(
    *,
    disc_type: str,
    media_type: str,
    dir_label: str,
    disc_label: str | None = None,
    label: str | None = None,
    output: str,
    output_bd_audio: str | None = None,
) -> tuple[str, int]:
    """Resolve where a new rip lands.  Returns (out_dir, disc_num).

    - Music BDs go to a separate output root (like audio CDs go to
      /output-cd) when `output_bd_audio` is given; music DVDs land under
      {output}/music/rips/dvd.
    - TV box sets (MRROBOT_S2D1_NA, "Mr. Robot: Season Two (Disc 1)", ...)
      land at {series}/Season NN/Disc NN, bumping with a " (2)" suffix only
      when the target already has content (re-rip collision).
    - Everything else: {output}/{tv|movies|music}/rips/{media_type}/{label}/discN
      with auto-incrementing disc numbers.
    """
    box_set = None
    if disc_type == "tv":
        box_set = parse_season_disc(disc_label or "") or parse_season_disc(label or "")

    if disc_type == "music" and output_bd_audio:
        return _next_disc_dir(os.path.join(output_bd_audio, dir_label))

    if box_set:
        series_pretty, season_n, disc_n = box_set
        series_dir = sanitize_filename(series_pretty)
        label_dir = os.path.join(output, "tv", "rips", media_type, series_dir, f"Season {season_n:02d}")
        disc_leaf = f"Disc {disc_n:02d}"
        out_dir = os.path.join(label_dir, disc_leaf)
        # Re-rip collision: only bump if the target already has content.
        suffix = 2
        while os.path.isdir(out_dir) and any(os.scandir(out_dir)):
            out_dir = os.path.join(label_dir, f"{disc_leaf} ({suffix})")
            suffix += 1
        return out_dir, disc_n

    content_type = {"tv": "tv", "music": "music"}.get(disc_type, "movies")
    label_dir = os.path.join(output, content_type, "rips", media_type, dir_label)
    return _next_disc_dir(label_dir)


def _next_disc_dir(label_dir: str) -> tuple[str, int]:
    """First discN under label_dir that is absent OR an empty leftover dir.

    Empty dirs happen when a previous attempt got as far as creating the
    output dir but produced nothing (e.g. aborted before the plan was
    written) — reuse them instead of bumping to discN+1.  Anything with
    content (a plan, a manifest, MKVs) is a real disc and bumps.
    """
    disc_num = 1
    while True:
        out_dir = os.path.join(label_dir, f"disc{disc_num}")
        if not os.path.exists(out_dir):
            return out_dir, disc_num
        if os.path.isdir(out_dir) and not any(os.scandir(out_dir)):
            return out_dir, disc_num
        disc_num += 1


def seed_or_refresh_plan(
    *,
    out_dir: str,
    disc_id: str,
    id_type: str,
    disc_label: str | None,
    media_type: str,
    title_records: list[TitleRecord],
    classification: ClassificationRecord,
    existing: RipPlan | None,
    default_block: PlanBlock,
) -> RipPlan:
    """Write the plan for this disc and return it.

    An existing plan is authoritative — its `plan` and `identify` blocks
    survive verbatim (user edits always win); only `classification`,
    `titles`, and `scanned_at` refresh.  New discs seed `plan` from
    `default_block` (the classifier's pick, possibly env-overridden).
    """
    if existing is not None:
        plan_block = existing.plan
        identify_block = existing.identify
        # Refresh the informational derived fields on any URL matches the
        # user added while the disc was out.
        materialize_match_fields(identify_block)
    else:
        plan_block = default_block
        identify_block = None
    plan = build_plan(
        disc_id=disc_id,
        id_type=id_type,
        disc_label=disc_label,
        media_type=media_type,
        titles=title_records,
        classification=classification,
        plan_block=plan_block,
        identify=identify_block,
    )
    write_plan(out_dir, plan)
    print(f"  Plan: {plan_path(out_dir)}")
    return plan
