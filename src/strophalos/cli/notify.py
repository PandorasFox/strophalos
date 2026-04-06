"""CLI entry point for notify — send notifications via apprise."""

from __future__ import annotations

import argparse

from strophalos.core.notify import notify


def main() -> None:
    parser = argparse.ArgumentParser(description="Send notifications via apprise")
    parser.add_argument("--error", action="store_true", help="Send as failure/high priority")
    parser.add_argument("title", help="Notification title")
    parser.add_argument("body", nargs="?", default="", help="Notification body")
    args = parser.parse_args()

    notify(args.title, args.body, error=args.error)


if __name__ == "__main__":
    main()
