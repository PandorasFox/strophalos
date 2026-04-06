#!/bin/sh
# Probe the disc in the drive without ripping.
#
# Usage: ./helpers/probe.sh
#
# Set STROPHALOS_COMPOSE to the directory containing your compose.yml.

COMPOSE_DIR="${STROPHALOS_COMPOSE:?Set STROPHALOS_COMPOSE to your compose directory}"
docker compose -f "$COMPOSE_DIR/compose.yml" run --rm strophalos probe-disc
