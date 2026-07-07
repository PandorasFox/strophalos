"""Rip manifest lifecycle — handoff between rip and identify stages.

The .rip-manifest.json file is written to each disc output directory by the
orchestrator. It tracks rip status and provides metadata for the identify stage.

Identify mode finds completed rips by looking for manifests with status="done"
that lack a corresponding identification manifest.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

MANIFEST_FILENAME = ".rip-manifest.json"

IDENTIFY_MANIFESTS = (
    ".episode-manifest.json",
    ".movie-manifest.json",
    ".music-manifest.json",
)

CONFLICT_MANIFEST = ".identify-conflict.json"
IDENTIFY_STATE_FILENAME = ".identify-state.json"

# Retry windows (seconds)
_NO_TMDB_MATCH_RETRY = 24 * 3600

# External resources that can unblock a state.  Maps blocker name → file path.
# When the file's mtime is newer than the attempt timestamp, the state is retried.
BLOCKER_FILES = {
    "opensubtitles_token": Path("/config/opensubtitles.json"),
}


@dataclass
class RipManifest:
    # "planned": plan written, disc ejected for review, rip pending re-insert.
    # Identify mode ignores it (find_pending_rips only picks "done").
    status: str  # "planned" | "ripping" | "done" | "failed"
    label: str
    disc_type: str  # "tv" | "movie" | "music" | "data"
    media_type: str  # "dvd" | "bd" | "uhd" | "cd" | "data"
    expected_titles: int
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    title_count: int | None = None
    completed_at: str | None = None
    disc_id: str | None = None
    error: str | None = None
    # Music BD metadata (populated by rip_video when disc_type == "music")
    musicbrainz_release_id: str | None = None
    musicbrainz_artist: str | None = None
    musicbrainz_title: str | None = None
    mb_track_count: int | None = None
    matched_tracks: int | None = None
    match_strategy: str | None = None  # "play-all" | "rip-all-dedup-window"
    notes: str | None = None


def write_manifest(output_dir: Path, manifest: RipManifest) -> Path:
    """Write .rip-manifest.json to output_dir. Creates dir if needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / MANIFEST_FILENAME
    path.write_text(json.dumps(asdict(manifest), indent=2))
    return path


def read_manifest(output_dir: Path) -> RipManifest | None:
    """Read .rip-manifest.json from output_dir. Returns None if missing/invalid."""
    path = output_dir / MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return RipManifest(
            status=data["status"],
            label=data["label"],
            disc_type=data["disc_type"],
            media_type=data["media_type"],
            expected_titles=data["expected_titles"],
            started_at=data.get("started_at", ""),
            title_count=data.get("title_count"),
            completed_at=data.get("completed_at"),
            disc_id=data.get("disc_id"),
            musicbrainz_release_id=data.get("musicbrainz_release_id"),
            musicbrainz_artist=data.get("musicbrainz_artist"),
            musicbrainz_title=data.get("musicbrainz_title"),
            mb_track_count=data.get("mb_track_count"),
            matched_tracks=data.get("matched_tracks"),
            match_strategy=data.get("match_strategy"),
            notes=data.get("notes"),
            error=data.get("error"),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def update_manifest_done(output_dir: Path, title_count: int, disc_id: str | None = None) -> None:
    """Update existing manifest to status='done'."""
    manifest = read_manifest(output_dir)
    if manifest is None:
        return
    manifest.status = "done"
    manifest.title_count = title_count
    manifest.disc_id = disc_id
    manifest.completed_at = datetime.now().isoformat()
    write_manifest(output_dir, manifest)


def update_manifest_failed(output_dir: Path, error: str) -> None:
    """Update existing manifest to status='failed'."""
    manifest = read_manifest(output_dir)
    if manifest is None:
        return
    manifest.status = "failed"
    manifest.error = error
    manifest.completed_at = datetime.now().isoformat()
    write_manifest(output_dir, manifest)


def write_conflict(output_dir: Path, conflicts: list[dict]) -> Path:
    """Write a conflict marker so the identify loop stops re-notifying.

    Each entry in *conflicts* should have ``library_path`` and ``inode`` keys
    describing the existing library file that caused the conflict.
    """
    path = output_dir / CONFLICT_MANIFEST
    data = {"conflicts": conflicts, "timestamp": datetime.now().isoformat()}
    path.write_text(json.dumps(data, indent=2))
    return path


def _conflict_still_valid(output_dir: Path) -> bool:
    """Return True if the conflict marker exists and every conflict is unresolved.

    A conflict is resolved when the library file no longer exists or its inode
    has changed (the user replaced/removed the blocking file).
    """
    path = output_dir / CONFLICT_MANIFEST
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    for c in data.get("conflicts", []):
        lib_path = Path(c["library_path"])
        if not lib_path.exists():
            # File removed — conflict resolved
            path.unlink(missing_ok=True)
            return False
        if lib_path.stat().st_ino != c["inode"]:
            # Inode changed — user replaced the file
            path.unlink(missing_ok=True)
            return False
    return True


def compute_inputs_fingerprint(rip_dir: Path) -> str:
    """SHA1 over (filename, size) of MKV files in *rip_dir*.

    Detects when the rip's input set changes — added/removed/replaced files —
    without being noisy about touch-only mtime updates.
    """
    parts: list[str] = []
    for mkv in sorted(rip_dir.glob("*_t[0-9][0-9].mkv")):
        try:
            parts.append(f"{mkv.name}:{mkv.stat().st_size}")
        except OSError:
            parts.append(f"{mkv.name}:?")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def read_identify_state(rip_dir: Path) -> dict | None:
    path = rip_dir / IDENTIFY_STATE_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_identify_state(
    rip_dir: Path,
    *,
    status: str,
    fingerprint: str,
    summary: str,
    blocker: str | None = None,
) -> None:
    data = {
        "status": status,
        "blocker": blocker,
        "inputs_fingerprint": fingerprint,
        "attempted_at": datetime.now().isoformat(),
        "summary": summary,
    }
    path = rip_dir / IDENTIFY_STATE_FILENAME
    path.write_text(json.dumps(data, indent=2))


def settle_identify(
    rip_dir: Path,
    *,
    status: str,
    summary: str,
    blocker: str | None = None,
    notify_title: str | None = None,
    notify_body: str | None = None,
    notify_error: bool = False,
) -> None:
    """Persist the identify outcome and, on state transitions only, notify.

    A "transition" is any change in ``status``, ``summary``, or
    ``inputs_fingerprint`` versus the previous ``.identify-state.json``.
    This is what keeps the user's notification channel from receiving the
    same message every time the daemon's identify loop re-runs.
    """
    from strophalos.core.notify import notify as _notify

    fingerprint = compute_inputs_fingerprint(rip_dir)
    prior = read_identify_state(rip_dir)
    is_new = (
        prior is None
        or prior.get("status") != status
        or prior.get("summary") != summary
        or prior.get("inputs_fingerprint") != fingerprint
    )

    if is_new and notify_title is not None:
        _notify(notify_title, notify_body or "", error=notify_error)

    write_identify_state(
        rip_dir,
        status=status,
        fingerprint=fingerprint,
        summary=summary,
        blocker=blocker,
    )


def _state_blocks_retry(rip_dir: Path) -> bool:
    """Return True if a prior identify attempt settled this rip — skip it.

    Returns False (retry) when:
      - no state file exists
      - the current input fingerprint differs from the recorded one
      - status is retry-worthy and its retry condition is met:
          * blocked: the blocker resource (e.g. OS token file) mtime has advanced
          * no_tmdb_match: more than _NO_TMDB_MATCH_RETRY seconds elapsed
    """
    state = read_identify_state(rip_dir)
    if state is None:
        return False

    if state.get("inputs_fingerprint") != compute_inputs_fingerprint(rip_dir):
        return False

    status = state.get("status")
    attempted_at = state.get("attempted_at", "")

    if status == "blocked":
        blocker = state.get("blocker")
        blocker_path = BLOCKER_FILES.get(blocker) if blocker else None
        if blocker_path and blocker_path.exists():
            try:
                attempted_ts = datetime.fromisoformat(attempted_at).timestamp()
            except ValueError:
                return False
            if blocker_path.stat().st_mtime > attempted_ts:
                return False
        return True

    if status == "no_tmdb_match":
        try:
            attempted_ts = datetime.fromisoformat(attempted_at).timestamp()
        except ValueError:
            return False
        if time.time() - attempted_ts > _NO_TMDB_MATCH_RETRY:
            return False
        return True

    # success / partial / no_match → settled until inputs change
    return True


def find_pending_rips(archive_root: Path) -> list[tuple[Path, RipManifest]]:
    """Find rip directories with status='done' that lack identification manifests."""
    results = []
    for manifest_path in archive_root.rglob(MANIFEST_FILENAME):
        manifest = read_manifest(manifest_path.parent)
        if manifest is None or manifest.status != "done":
            continue
        # Skip data and music discs — no identification step
        # (audio CDs/BDs are fully handled by the rip stage)
        if manifest.disc_type in ("data", "music"):
            continue
        rip_dir = manifest_path.parent
        has_id = any((rip_dir / f).exists() for f in IDENTIFY_MANIFESTS)
        if has_id:
            continue
        # Skip if there is an unresolved conflict (avoids notification spam)
        if _conflict_still_valid(rip_dir):
            continue
        # Skip if a prior identify attempt settled this rip and nothing has changed.
        if _state_blocks_retry(rip_dir):
            continue
        results.append((rip_dir, manifest))
    return results
