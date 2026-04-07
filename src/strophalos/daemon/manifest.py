"""Rip manifest lifecycle — handoff between rip and identify stages.

The .rip-manifest.json file is written to each disc output directory by the
orchestrator. It tracks rip status and provides metadata for the identify stage.

Identify mode finds completed rips by looking for manifests with status="done"
that lack a corresponding identification manifest.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

MANIFEST_FILENAME = ".rip-manifest.json"

IDENTIFY_MANIFESTS = (
    ".episode-manifest.json",
    ".movie-manifest.json",
    ".music-manifest.json",
)


@dataclass
class RipManifest:
    status: str  # "ripping" | "done" | "failed"
    label: str
    disc_type: str  # "tv" | "movie" | "music" | "data"
    media_type: str  # "dvd" | "bd" | "uhd" | "cd" | "data"
    expected_titles: int
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    title_count: int | None = None
    completed_at: str | None = None
    disc_id: str | None = None
    error: str | None = None


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


def find_pending_rips(archive_root: Path) -> list[tuple[Path, RipManifest]]:
    """Find rip directories with status='done' that lack identification manifests."""
    results = []
    for manifest_path in archive_root.rglob(MANIFEST_FILENAME):
        manifest = read_manifest(manifest_path.parent)
        if manifest is None or manifest.status != "done":
            continue
        # Skip data discs — no identification step
        if manifest.disc_type == "data":
            continue
        rip_dir = manifest_path.parent
        has_id = any((rip_dir / f).exists() for f in IDENTIFY_MANIFESTS)
        if not has_id:
            results.append((rip_dir, manifest))
    return results
