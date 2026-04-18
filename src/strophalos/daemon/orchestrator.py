"""Main orchestrator — disc polling loop and pipeline mode dispatch."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path

from strophalos.core.notify import notify
from strophalos.daemon.drive import check_disc_present, check_media_changed, eject_disc
from strophalos.daemon.manifest import (
    RipManifest,
    find_pending_rips,
    write_manifest,
)
from strophalos.ripper.probe import ProbeResult, probe_disc
from strophalos.ripper.result import RipResult


class PipelineMode(Enum):
    PROBE = "probe"
    SCAN = "scan"
    RIP = "rip"
    IDENTIFY = "identify"
    FULL = "full"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[strophalos] {ts} {msg}", flush=True)


def _run_hook(name: str, *args: str) -> None:
    hook = Path("/config/hooks") / name
    if hook.is_file():
        _log(f"hook: {name}")
        try:
            subprocess.run(["sh", str(hook)] + list(args), timeout=60)
        except Exception:
            pass


class Orchestrator:
    def __init__(
        self,
        device: str,
        mode: PipelineMode,
        poll_interval: int,
        eject_on_complete: bool,
        puid: int,
        pgid: int,
        archive_root: str = "/media/archive",
        library_root: str = "/media",
    ) -> None:
        self.device = device
        self.mode = mode
        self.poll_interval = poll_interval
        self.eject_on_complete = eject_on_complete
        self.puid = puid
        self.pgid = pgid
        self.archive_root = Path(archive_root)
        self.library_root = Path(library_root)
        self._last_disc = ""
        self._disc_was_present = False
        self._tick = 0

    def run(self) -> None:
        _log(f"strophalos starting — device={self.device} mode={self.mode.value} poll={self.poll_interval}s")

        if self.mode == PipelineMode.IDENTIFY:
            self._run_identify_loop()
            return

        self._run_disc_loop()

    def _run_disc_loop(self) -> None:
        # Drain any stale media-changed flag at startup.
        # We only trust media-changed events, never bare drive status,
        # because some drives (e.g. ASUS BW-16D1HT) report CDS_DISC_OK
        # even with the tray open.
        check_media_changed(self.device)
        _log("waiting for disc insertion (media-changed trigger)...")

        while True:
            time.sleep(self.poll_interval)
            self._tick += 1

            # Heartbeat every ~60s
            if self._tick % max(1, 60 // self.poll_interval) == 0:
                status = check_disc_present(self.device)
                _log(f"heartbeat (tick={self._tick}, disc_present={self._disc_was_present}, drive_ready={status})")

            media_changed = check_media_changed(self.device)

            if not media_changed:
                continue

            # Media changed — could be insert or eject.
            # Wait for spin-up then check if a disc is actually ready.
            _log("media change detected, waiting for spin-up...")
            time.sleep(5)

            if not check_disc_present(self.device):
                _log("media changed but no disc ready (eject)")
                self._disc_was_present = False
                self._last_disc = ""
                continue

            self._disc_was_present = True
            self._handle_disc()

    def _handle_disc(self) -> None:
        _log("disc detected, probing...")
        probe = probe_disc(self.device)
        _log(f"probe: type={probe.disc_type} label='{probe.label}'")

        if self.mode == PipelineMode.PROBE:
            notify("Disc detected", f"{probe.label} ({probe.disc_type})")
            self._finish_disc(probe)
            return

        if self.mode == PipelineMode.SCAN:
            self._run_scan(probe)
            self._finish_disc(probe)
            return

        # RIP or FULL mode
        result = self._run_rip(probe)

        if result and self.mode == PipelineMode.FULL:
            # Background identification so next disc isn't blocked
            t = threading.Thread(target=self._run_identify_result, args=(result,), daemon=True)
            t.start()

        status = "SUCCESS" if result else "FAILURE"
        _run_hook(
            "disc_rip_terminated.sh",
            "0",
            probe.label,
            result.output_dir if result else "",
            status,
        )

        self._finish_disc(probe)

    def _run_scan(self, probe: ProbeResult) -> None:
        """Scan and classify without ripping (dry-run)."""
        if probe.disc_type == "dvd":
            from strophalos.cli.rip_dvd import rip_dvd_disc

            _log("scanning DVD via lsdvd...")
            rip_dvd_disc(self.device, probe.label, str(self.archive_root), dry_run=True)
        elif probe.disc_type in ("bluray", "unknown"):
            from strophalos.cli.rip_video import rip_video_disc

            _log("scanning Blu-ray via makemkvcon (this can take a few minutes)...")
            rip_video_disc(0, probe.label, str(self.archive_root), dry_run=True)
        elif probe.disc_type in ("data", "audio+data"):
            from strophalos.cli.rip_data import rip_data_disc

            rip_data_disc(self.device, probe.label, str(self.archive_root) + "/iso", dry_run=True)
        elif probe.disc_type == "audio":
            _log("audio CD — no scan beyond probe")

    def _run_rip(self, probe: ProbeResult) -> RipResult | None:
        """Run the appropriate ripper. Writes manifest before/after."""
        if probe.disc_type == "audio":
            return self._rip_audio(probe)
        if probe.disc_type == "audio+data":
            return self._rip_audio_data(probe)
        if probe.disc_type == "data":
            return self._rip_data(probe)
        if probe.disc_type == "dvd":
            return self._rip_dvd(probe)
        # bluray or unknown — makemkvcon
        return self._rip_video(probe)

    def _rip_video(self, probe: ProbeResult) -> RipResult | None:
        from strophalos.cli.rip_video import rip_video_disc

        notify("Ripping Blu-ray", probe.label)

        result = rip_video_disc(0, probe.label, str(self.archive_root), dry_run=False)
        if result is None:
            notify("Disc rip failed", probe.label, error=True)
            return None

        self._write_done_manifest(result)

        notify(
            "Disc ripped",
            f"{probe.label} — {result.title_count} title(s), {result.disc_type} ({result.media_type})",
        )
        return result

    def _rip_dvd(self, probe: ProbeResult) -> RipResult | None:
        from strophalos.cli.rip_dvd import rip_dvd_disc

        notify("Ripping DVD", probe.label)

        result = rip_dvd_disc(self.device, probe.label, str(self.archive_root), dry_run=False)
        if result is None:
            notify("DVD rip failed", probe.label, error=True)
            return None

        self._write_done_manifest(result)

        notify(
            "Disc ripped",
            f"{probe.label} — {result.title_count} title(s), {result.disc_type} (dvd)",
        )
        return result

    def _rip_data(self, probe: ProbeResult) -> RipResult | None:
        from strophalos.cli.rip_data import rip_data_disc

        result = rip_data_disc(self.device, probe.label, str(self.archive_root) + "/iso", dry_run=False)
        return result  # rip_data handles its own notifications

    def _rip_audio(self, probe: ProbeResult) -> RipResult | None:
        from strophalos.cli.rip_cd import rip_audio_cd

        _log("audio CD — handing off to whipper")
        rc = rip_audio_cd(self.device)
        if rc != 0:
            _log(f"CD rip failed (rc={rc})")
        return None  # Audio CDs don't go through identification

    def _rip_audio_data(self, probe: ProbeResult) -> RipResult | None:
        from strophalos.cli.rip_cd import rip_audio_cd
        from strophalos.cli.rip_data import rip_data_disc

        _log("audio+data disc — ripping ISO + audio tracks")
        notify("Ripping audio+data disc", probe.label)
        rip_data_disc(self.device, probe.label, str(self.archive_root) + "/iso", dry_run=False)
        rip_audio_cd(self.device)
        return None  # No identification for audio+data

    def _chown_output(self, path: Path) -> None:
        """Recursively chown output directory to PUID:PGID."""
        try:
            for root, _dirs, files in os.walk(path):
                os.chown(root, self.puid, self.pgid)
                for f in files:
                    os.chown(os.path.join(root, f), self.puid, self.pgid)
        except OSError as e:
            _log(f"chown failed for {path}: {e}")

    def _write_done_manifest(self, result: RipResult) -> None:
        """Chown output to PUID:PGID, then write completed manifest."""
        out_dir = Path(result.output_dir)
        self._chown_output(out_dir)
        mb = result.mb_metadata or {}
        manifest = RipManifest(
            status="done",
            label=result.label,
            disc_type=result.disc_type,
            media_type=result.media_type,
            expected_titles=result.title_count,
            title_count=result.title_count,
            completed_at=datetime.now().isoformat(),
            disc_id=result.disc_id,
            musicbrainz_release_id=mb.get("id"),
            musicbrainz_artist=mb.get("artist"),
            musicbrainz_title=mb.get("title"),
            mb_track_count=mb.get("track_count"),
            matched_tracks=mb.get("track_count"),  # filled by gap-fill step
            match_strategy=mb.get("match_method"),
            notes="MKV files contain video + lossless PCM audio. Convert to FLAC with: ffmpeg -i track.mkv -vn -c:a flac output.flac" if result.disc_type == "music" else None,
        )
        write_manifest(out_dir, manifest)

    def _run_identify_result(self, result: RipResult) -> None:
        """Run identification for a just-ripped disc (background thread)."""
        self._identify(result.disc_type, result.output_dir, result.label)

    def _identify(self, disc_type: str, output_dir: str, label: str) -> None:
        """Run the appropriate identifier via subprocess."""
        library = str(self.library_root)

        if disc_type == "tv":
            _log("TV disc — starting episode identification")
            cmd = ["identify-episodes", "--dir", output_dir, "--label", label, "--library", library]
        elif disc_type == "music":
            _log("audio disc — starting music identification")
            cmd = ["identify-music", "--dir", output_dir, "--label", label, "--library", library]
        else:
            _log("movie disc — starting identification")
            cmd = ["identify-movie", "--dir", output_dir, "--label", label, "--library", library]

        try:
            subprocess.run(cmd, timeout=3600)
        except subprocess.TimeoutExpired:
            _log(f"identification timed out for {output_dir}")
        except Exception as e:
            _log(f"identification failed for {output_dir}: {e}")

    def _run_identify_loop(self) -> None:
        """Identify mode: poll archive for unidentified completed rips."""
        _log(f"identify mode — watching {self.archive_root} for completed rips")

        while True:
            pending = find_pending_rips(self.archive_root)
            if pending:
                _log(f"found {len(pending)} unidentified rip(s)")

            for rip_dir, manifest in pending:
                _log(f"identifying {rip_dir} (type={manifest.disc_type}, label={manifest.label})")
                try:
                    self._identify(manifest.disc_type, str(rip_dir), manifest.label)
                except Exception as e:
                    _log(f"identification failed for {rip_dir}: {e}")

            time.sleep(60)

    def _finish_disc(self, probe: ProbeResult) -> None:
        self._last_disc = f"{probe.disc_type}:{probe.label}"
        if self.eject_on_complete:
            _log("ejecting")
            if not eject_disc(self.device):
                _log("eject failed")
            self._last_disc = ""
            self._disc_was_present = False
        _log("ready for next disc")
