#!/bin/sh
# Scan the disc and show title list without ripping.
#
# Usage:
#   ./helpers/scan.sh              — auto-detect disc type
#   ./helpers/scan.sh dvd          — force DVD scan (lsdvd)
#   ./helpers/scan.sh bluray       — force Blu-ray scan (makemkvcon)
#
# Set STROPHALOS_COMPOSE to the directory containing your compose.yml.

COMPOSE_DIR="${STROPHALOS_COMPOSE:?Set STROPHALOS_COMPOSE to your compose directory}"
RUN="docker compose -f $COMPOSE_DIR/compose.yml run --rm strophalos"
TYPE="${1:-auto}"

if [ "$TYPE" = "auto" ]; then
    TYPE=$($RUN probe-disc 2>/dev/null | grep '^DISC_TYPE=' | cut -d= -f2-)
    echo "Detected: $TYPE"
fi

case "$TYPE" in
    dvd)     $RUN rip-dvd --dry-run ;;
    bluray)  $RUN rip-video --dry-run --drive 0 ;;
    audio)   echo "Audio CD — no title scan (use probe.sh for disc ID)" ;;
    data)    $RUN sh -c 'echo "Data disc: $(blockdev --getsize64 $DEVICE 2>/dev/null || echo unknown) bytes"' ;;
    *)       echo "Unknown type: $TYPE"; exit 1 ;;
esac
