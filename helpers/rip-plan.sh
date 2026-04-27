#!/bin/sh
# Dump or show the per-disc rip plan without ripping.
#
# Usage:
#   ./helpers/rip-plan.sh                    — scan disc, write/refresh plan JSON
#   ./helpers/rip-plan.sh --show DISC_ID     — pretty-print an existing plan
#   ./helpers/rip-plan.sh --show /mnt/.../rip-dir
#
# After a scan, edit the plan file (set "manual_override": true and adjust
# "titles_to_rip"), then reinsert the disc — the next rip uses the override
# and re-rips into the same directory.
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
