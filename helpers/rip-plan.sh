#!/bin/sh
# Dump, show, or validate the per-disc rip plan without ripping.
#
# Usage:
#   ./helpers/rip-plan.sh                        — scan disc, write/refresh plan JSON
#   ./helpers/rip-plan.sh --show DISC_ID         — pretty-print an existing plan
#   ./helpers/rip-plan.sh --show /mnt/.../rip-dir
#   ./helpers/rip-plan.sh --validate DISC_ID     — check an edited plan before re-insert
#
# The daemon writes the plan automatically on first insertion and ejects.
# Edit "titles_to_rip" / "disc_type" / "identify" (URL pins + per-title
# matches) while the disc is out, then reinsert — the rip follows the plan
# into the same directory.
#
# Env:
#   STROPHALOS_COMPOSE  directory containing compose.yml (required)
#   STROPHALOS_MEDIA    host path mapped to /media in the container
#                       (default: /mnt/cerberus/library) — used to translate
#                       container-relative paths in the printed output.

COMPOSE_DIR="${STROPHALOS_COMPOSE:?Set STROPHALOS_COMPOSE to your compose directory}"
HOST_MEDIA="${STROPHALOS_MEDIA:-/mnt/cerberus/library}"
RUN="docker compose -f $COMPOSE_DIR/compose.yml run --rm strophalos"

# Translate host-style --show arguments to container paths before dispatch.
args=""
for a in "$@"; do
    case "$a" in
        "$HOST_MEDIA"|"$HOST_MEDIA"/*)
            a="/media${a#$HOST_MEDIA}"
            ;;
    esac
    args="$args $a"
done

# Pipe container output through sed to show host paths to the user.
# shellcheck disable=SC2086
$RUN rip-plan $args | sed "s|/media/|${HOST_MEDIA}/|g"
