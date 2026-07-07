"""Per-disc-id rip plan — editable JSON manifest.

The plan file lives at `{rip_dir}/.rip-plan.json`, alongside the ripped MKVs
and the existing `.rip-manifest.json` / `.disc-id.json` artifacts.

Lifecycle (two-pass):
  - First insertion of a disc: the daemon scans, the classifier seeds
    `plan` (disc_type + titles_to_rip), the file is written, and the disc
    is ejected WITHOUT ripping.
  - While the disc is out, the user optionally edits the plan:
    `plan.titles_to_rip` / `plan.disc_type` drive the rip;
    `identify.url` (or legacy `identify.tmdb_id`) pins the whole-disc
    match; `identify.matches` maps individual titles to TMDb/MusicBrainz
    entries by URL (multi-movie discs, pinned music releases).
  - Re-insertion of the same disc (matched by `disc_id`): the plan is
    validated and the rip runs per its `plan` block.  The existing
    `plan` and `identify` blocks are authoritative — user edits always
    win.  The `classification`, `titles`, and `scanned_at` fields are
    refreshed each scan; they're informational and surface disc-state
    drift.
  - Reset to classifier defaults: `rm <dir>/.rip-plan.json` and re-insert
    (the next insertion re-plans and ejects again).

In other words: the plan file's existence IS the "validated" signal.
There is no separate approval flag to flip.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

PLAN_FILENAME = ".rip-plan.json"


class PlanValidationError(Exception):
    """A plan failed URL parsing or consistency validation.

    The message lists every problem found, one per line, so the user can
    fix the JSON in one editing pass.
    """


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
    note: str | None = None


@dataclass(frozen=True)
class ParsedUrl:
    """A provider URL decomposed into an addressable entry."""

    provider: str  # "tmdb" | "musicbrainz"
    kind: str  # "movie" | "tv" | "release"
    id: str  # numeric string (tmdb) or release UUID (musicbrainz)
    season: int | None = None  # from a .../tv/<id>/season/<n> URL


_TMDB_URL_RE = re.compile(
    r"^https?://(?:www\.)?themoviedb\.org"
    r"/(movie|tv)/(\d+)(?:-[^/]*)?"
    r"(?:/season/(\d+))?/?$"
)
# Any host with a /release/<uuid> path is treated as MusicBrainz — release
# UUIDs are portable across mirrors, so URLs pasted from a self-hosted MB
# server pin the same release as musicbrainz.org ones.
_MB_RELEASE_URL_RE = re.compile(
    r"^https?://[^/]+/release/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.IGNORECASE,
)


def parse_provider_url(url: str) -> ParsedUrl:
    """Parse a TMDb or MusicBrainz URL into (provider, kind, id[, season]).

    Raises PlanValidationError for anything unrecognized — unknown providers
    must fail loudly rather than be silently ignored at identify time.
    """
    m = _TMDB_URL_RE.match(url.strip())
    if m:
        kind, tmdb_id, season = m.group(1), m.group(2), m.group(3)
        if season is not None and kind != "tv":
            raise PlanValidationError(f"season path on a non-tv TMDb URL: {url}")
        return ParsedUrl("tmdb", kind, tmdb_id, int(season) if season else None)
    m = _MB_RELEASE_URL_RE.match(url.strip())
    if m:
        return ParsedUrl("musicbrainz", "release", m.group(1).lower())
    raise PlanValidationError(
        f"unrecognized provider URL: {url!r} — expected a themoviedb.org "
        "movie/tv URL or a MusicBrainz /release/<uuid> URL"
    )


@dataclass
class TitleMatch:
    """Maps ripped title(s) to one provider entry — `identify.matches[]`.

    `url` is authoritative (the user pastes it); `provider`/`kind`/`id`/
    `season` are derived at read time and materialized on rewrite for
    humans reading the JSON.  An empty `titles` list means "the whole
    disc" (the natural form for a music release pin).
    """

    url: str
    titles: list[int] = field(default_factory=list)
    provider: str | None = None
    kind: str | None = None
    id: str | None = None
    season: int | None = None
    note: str | None = None


@dataclass
class IdentifyOverride:
    """Pinned identify match — bypasses search in the identify-* CLIs.

    Whole-disc pin: set `url` (TMDb movie/tv or MusicBrainz release URL) —
    or the legacy `tmdb_id` + `tmdb_type` pair.  For TV, `season`
    optionally overrides the season parsed from the disc label (useful when
    the label doesn't carry season info, or when the disc spans a different
    season than the label implies — e.g. a miniseries you want pinned to S1).

    Per-title pins: `matches` maps subsets of `plan.titles_to_rip` to
    provider entries — this is how a dual-feature disc becomes two movies.
    """

    tmdb_id: int | None = None
    tmdb_type: str | None = None  # "movie" | "tv"
    season: int | None = None
    note: str | None = None
    url: str | None = None
    matches: list[TitleMatch] = field(default_factory=list)


def effective_pin(identify: IdentifyOverride) -> ParsedUrl | None:
    """Resolve the whole-disc pin: `url` wins, legacy tmdb fields fall back.

    Raises PlanValidationError if both are set and disagree, or the URL is
    unparseable.  Returns None when nothing is pinned.
    """
    if identify.url:
        parsed = parse_provider_url(identify.url)
        if identify.tmdb_id is not None and (
            parsed.provider != "tmdb"
            or parsed.id != str(identify.tmdb_id)
            or (identify.tmdb_type and parsed.kind != identify.tmdb_type)
        ):
            raise PlanValidationError(
                f"identify.url ({parsed.provider} {parsed.kind} {parsed.id}) disagrees with "
                f"legacy identify.tmdb_id={identify.tmdb_id}/tmdb_type={identify.tmdb_type!r} — "
                "remove one of them"
            )
        season = parsed.season if parsed.season is not None else identify.season
        return ParsedUrl(parsed.provider, parsed.kind, parsed.id, season)
    if identify.tmdb_id is not None:
        return ParsedUrl("tmdb", identify.tmdb_type or "", str(identify.tmdb_id), identify.season)
    return None


def pinned_release_id(identify: IdentifyOverride) -> str | None:
    """The MusicBrainz release UUID pinned on this plan, if any.

    Checks the whole-disc pin first, then `matches` (a release match's
    `titles` list is ignored — a release always covers the whole disc).
    Unparseable URLs are skipped here; validate_plan reports them.
    """
    try:
        pin = effective_pin(identify)
    except PlanValidationError:
        pin = None
    if pin and pin.provider == "musicbrainz":
        return pin.id
    for m in identify.matches:
        try:
            parsed = parse_provider_url(m.url)
        except PlanValidationError:
            continue
        if parsed.kind == "release":
            return parsed.id
    return None


def materialize_match_fields(identify: IdentifyOverride) -> None:
    """Fill the derived provider/kind/id/season fields on each parseable
    match in place (informational — shown to humans reading the JSON).
    Unparseable URLs are left untouched; validate_plan reports them."""
    for m in identify.matches:
        try:
            parsed = parse_provider_url(m.url)
        except PlanValidationError:
            continue
        m.provider = parsed.provider
        m.kind = parsed.kind
        m.id = parsed.id
        if parsed.season is not None:
            m.season = parsed.season


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


_PLAN_BLOCK_FIELDS = {f.name for f in fields(PlanBlock)}
_IDENTIFY_FIELDS = {f.name for f in fields(IdentifyOverride)}
_TITLE_MATCH_FIELDS = {f.name for f in fields(TitleMatch)}


def _identify_from_dict(raw: dict) -> IdentifyOverride:
    raw = {k: v for k, v in raw.items() if k in _IDENTIFY_FIELDS}
    matches_raw = raw.pop("matches", None) or []
    matches = [TitleMatch(**{k: v for k, v in m.items() if k in _TITLE_MATCH_FIELDS}) for m in matches_raw]
    return IdentifyOverride(matches=matches, **raw)


def _plan_from_dict(data: dict) -> RipPlan:
    # Legacy plan files (pre-cleanup) had `manual_override` and `forced_rip_all`
    # keys on the plan block; drop any unknown keys so old files still load.
    plan_raw = {k: v for k, v in data["plan"].items() if k in _PLAN_BLOCK_FIELDS}
    return RipPlan(
        disc_id=data["disc_id"],
        id_type=data["id_type"],
        disc_label=data.get("disc_label"),
        media_type=data["media_type"],
        scanned_at=data["scanned_at"],
        titles=[TitleRecord(**t) for t in data.get("titles", [])],
        classification=ClassificationRecord(**data["classification"]),
        plan=PlanBlock(**plan_raw),
        identify=_identify_from_dict(data.get("identify") or {}),
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


@dataclass
class PlanHit:
    """Result of a disc_id plan lookup.

    `plan` is None when the file matched the disc_id but failed to load —
    `error` says why.  Surfacing that instead of skipping prevents a
    typo'd plan from silently triggering a fresh first-pass into a new
    discN directory.
    """

    rip_dir: Path
    plan: RipPlan | None
    error: str | None = None


def locate_plan(
    archive_roots: str | Path | Iterable[str | Path],
    disc_id: str,
) -> PlanHit | None:
    """Walk archive root(s) for a `.rip-plan.json` whose `disc_id` matches.

    Accepts a single path or an iterable of paths — music BDs and videos
    can live under different mounts (e.g. /media/archive vs /output-bd).
    Returns a PlanHit for the first raw-JSON disc_id match, or None.
    Files whose JSON can't be parsed at all are skipped (their disc_id is
    unreadable, so they can't be attributed to this disc).
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
            if data.get("disc_id") != disc_id:
                continue
            try:
                return PlanHit(path.parent, _plan_from_dict(data))
            except (KeyError, TypeError) as e:
                err = f"plan file at {path} matched disc_id but failed to load: {e!r}"
                return PlanHit(path.parent, None, error=err)
    return None


def find_plan_by_disc_id(
    archive_roots: str | Path | Iterable[str | Path],
    disc_id: str,
) -> tuple[Path, RipPlan] | None:
    """Compat wrapper over locate_plan: (rip_dir, plan) or None."""
    hit = locate_plan(archive_roots, disc_id)
    if hit is None or hit.plan is None:
        return None
    return hit.rip_dir, hit.plan


def validate_plan(plan: RipPlan, scanned_tids: set[int] | None = None) -> None:
    """Consistency-check a plan; raise PlanValidationError listing ALL problems.

    Checks the whole-disc pin, every `identify.matches` entry (URL parse,
    kind vs plan.disc_type, title membership, duplicate title claims), and —
    when `scanned_tids` is given (pass 2, disc in drive) — that every
    `plan.titles_to_rip` entry actually exists on the disc.
    """
    errors: list[str] = []
    try:
        effective_pin(plan.identify)
    except PlanValidationError as e:
        errors.append(str(e))

    claimed: dict[int, str] = {}
    for i, m in enumerate(plan.identify.matches):
        label = f"identify.matches[{i}]"
        try:
            parsed = parse_provider_url(m.url)
        except PlanValidationError as e:
            errors.append(f"{label}: {e}")
            continue
        if parsed.kind == "release" and plan.plan.disc_type != "music":
            errors.append(
                f"{label}: MusicBrainz release URL but plan.disc_type is {plan.plan.disc_type!r} — set it to 'music'"
            )
        elif parsed.kind in ("movie", "tv") and plan.plan.disc_type == "music":
            errors.append(f"{label}: TMDb {parsed.kind} URL but plan.disc_type is 'music'")
        for tid in m.titles:
            if tid not in plan.plan.titles_to_rip:
                errors.append(f"{label}: title {tid} is not in plan.titles_to_rip {plan.plan.titles_to_rip}")
            if tid in claimed:
                errors.append(f"{label}: title {tid} already claimed by {claimed[tid]}")
            else:
                claimed[tid] = label

    if scanned_tids is not None:
        missing = [t for t in plan.plan.titles_to_rip if t not in scanned_tids]
        if missing:
            errors.append(
                f"plan.titles_to_rip contains title(s) {missing} not present on the "
                f"disc; available titles: {sorted(scanned_tids)}"
            )

    if errors:
        raise PlanValidationError("\n".join(errors))


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
