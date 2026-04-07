"""CLI entry point for strophalos-daemon — main orchestrator."""

from __future__ import annotations

import argparse
import os
import signal
import sys

from strophalos.daemon.init import initialize
from strophalos.daemon.orchestrator import Orchestrator, PipelineMode


def main() -> None:
    parser = argparse.ArgumentParser(description="strophalos disc ripper daemon")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PipelineMode],
        default=os.environ.get("STROPHALOS_MODE", "full"),
        help="Pipeline mode (default: $STROPHALOS_MODE or 'full')",
    )
    parser.add_argument("--device", default=os.environ.get("DEVICE", "/dev/sr1"))
    parser.add_argument("--poll-interval", type=int, default=int(os.environ.get("POLL_INTERVAL", "5")))
    parser.add_argument("--no-eject", action="store_true")
    parser.add_argument("--archive", default="/media/archive", help="Archive root directory")
    parser.add_argument("--library", default="/media", help="Library root directory")
    args = parser.parse_args()

    # Set environment expected by downstream tools
    os.environ.setdefault("HOME", "/config")

    puid = int(os.environ.get("PUID", "1000"))
    pgid = int(os.environ.get("PGID", "1000"))
    eject = not args.no_eject and os.environ.get("EJECT_ON_COMPLETE", "1") == "1"

    # Graceful shutdown on SIGTERM (docker stop)
    def _handle_sigterm(signum: int, frame: object) -> None:
        print("[strophalos] received SIGTERM, shutting down", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Initialize as root — stay root for probe (needs mount).
    # Rip/identify commands handle their own privilege dropping.
    initialize(puid, pgid)

    orchestrator = Orchestrator(
        device=args.device,
        mode=PipelineMode(args.mode),
        poll_interval=args.poll_interval,
        eject_on_complete=eject,
        puid=puid,
        pgid=pgid,
        archive_root=args.archive,
        library_root=args.library,
    )
    orchestrator.run()


if __name__ == "__main__":
    main()
