"""Per-disc-id rip plan — editable JSON manifest with manual override.

The plan file lives at `{rip_dir}/.rip-plan.json`, alongside the ripped MKVs
and the existing `.rip-manifest.json` / `.disc-id.json` artifacts.  The
classifier writes it on every rip; the user can edit the `plan` block
(set `manual_override: true`, change `titles_to_rip` / `disc_type`) and
reinsert the disc to drive a re-rip with the override.

On reinsertion we recompute `disc_id` and walk the archive for an existing
plan with a matching `disc_id` — if found with `manual_override`, we rip
into the same directory (replacing the previous MKVs) using the edited
title list.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

PLAN_FILENAME = ".rip-plan.json"


@dataclass
class TitleRecord:
    tid: int
    duration_seconds: int
    duration: str
    chapters: int | None = None
    size_bytes: int | None = None
    size_gb: float | None = None
    source_filename: str | None = None
    segment_map: str | None = None
    dropped: str | None = None  # "segment-duplicate-of-N" | "low-bitrate-..." | None


@dataclass
class ClassificationRecord:
    disc_type: str
    reason: str
    suggested_titles_to_rip: list[int]


@dataclass
class PlanBlock:
    disc_type: str
    titles_to_rip: list[int]
    manual_override: bool = False
    forced_rip_all: bool = False
    note: str | None = None


@dataclass
class IdentifyOverride:
    """Pinned TMDB match — bypasses search in identify-movie/identify-episodes.

    Set `tmdb_id` + `tmdb_type` to skip TMDb search entirely.  For TV, `season`
    optionally overrides the season parsed from the disc label (useful when the
    label doesn't carry season info, or when the disc spans a different season
    than the label implies — e.g. a miniseries you want pinned to S1).
    """

    tmdb_id: int | None = None
    tmdb_type: str | None = None  # "movie" | "tv"
    season: int | None = None
    note: str | None = None


@dataclass
class RipPlan:
    disc_id: str
    id_type: str  # "bd_sha256" | "dvd_crc64"
    disc_label: str | None
    media_type: str
    scanned_at: str
    titles: list[TitleRecord]
    classification: ClassificationRecord
    plan: PlanBlock
    identify: IdentifyOverride = field(default_factory=IdentifyOverride)


def plan_path(rip_dir: str | Path) -> Path:
    return Path(rip_dir) / PLAN_FILENAME


def _format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def build_title_records(
    durations: dict[int, int],
    chapters: dict[int, int],
    sizes: dict[int, int],
    source_filenames: dict[int, str],
    segment_maps: dict[int, str],
    dropped: dict[int, str] | None = None,
) -> list[TitleRecord]:
    """Build TitleRecord list from scan dicts.  `dropped` maps tid->reason."""
    dropped = dropped or {}
    all_tids = set(durations) | set(dropped)
    records: list[TitleRecord] = []
    for tid in sorted(all_tids):
        dur_s = durations.get(tid, 0)
        size_b = sizes.get(tid)
        records.append(
            TitleRecord(
                tid=tid,
                duration_seconds=dur_s,
                duration=_format_duration(dur_s),
                chapters=chapters.get(tid),
                size_bytes=size_b,
                size_gb=round(size_b / (1024**3), 3) if size_b else None,
                source_filename=source_filenames.get(tid),
                segment_map=segment_maps.get(tid),
                dropped=dropped.get(tid),
            )
        )
    return records


def _plan_from_dict(data: dict) -> RipPlan:
    identify_raw = data.get("identify") or {}
    return RipPlan(
        disc_id=data["disc_id"],
        id_type=data["id_type"],
        disc_label=data.get("disc_label"),
        media_type=data["media_type"],
        scanned_at=data["scanned_at"],
        titles=[TitleRecord(**t) for t in data.get("titles", [])],
        classification=ClassificationRecord(**data["classification"]),
        plan=PlanBlock(**data["plan"]),
        identify=IdentifyOverride(**identify_raw),
    )


def read_plan(rip_dir: str | Path) -> RipPlan | None:
    """Read plan from a rip dir; return None if missing or corrupt (logged)."""
    path = plan_path(rip_dir)
    if not path.exists():
        return None
    try:
        return _plan_from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"  rip plan: existing file at {path} is corrupt ({e}); will be overwritten")
        return None


def write_plan(rip_dir: str | Path, plan: RipPlan) -> Path:
    path = plan_path(rip_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(plan), indent=2))
    return path


def find_plan_by_disc_id(
    archive_roots: str | Path | Iterable[str | Path],
    disc_id: str,
) -> tuple[Path, RipPlan] | None:
    """Walk archive root(s) for a `.rip-plan.json` whose `disc_id` matches.

    Accepts a single path or an iterable of paths — music BDs and videos
    can live under different mounts (e.g. /media/archive vs /output-bd).
    Returns (rip_dir, plan) for the first match, or None.  Corrupt plans
    are skipped silently.
    """
    if isinstance(archive_roots, str | Path):
        roots: list[Path] = [Path(archive_roots)]
    else:
        roots = [Path(r) for r in archive_roots]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob(PLAN_FILENAME):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("disc_id") == disc_id:
                try:
                    return path.parent, _plan_from_dict(data)
                except (KeyError, TypeError):
                    continue
    return None


def build_plan(
    *,
    disc_id: str,
    id_type: str,
    disc_label: str | None,
    media_type: str,
    titles: list[TitleRecord],
    classification: ClassificationRecord,
    plan_block: PlanBlock,
    identify: IdentifyOverride | None = None,
) -> RipPlan:
    return RipPlan(
        disc_id=disc_id,
        id_type=id_type,
        disc_label=disc_label,
        media_type=media_type,
        scanned_at=datetime.now().isoformat(timespec="seconds"),
        titles=titles,
        classification=classification,
        plan=plan_block,
        identify=identify if identify is not None else IdentifyOverride(),
    )
