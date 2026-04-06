#!/bin/sh
# Rip a single disc (no auto-identification afterward).
#
# Usage:
#   ./helpers/rip.sh               — auto-detect and rip
#   ./helpers/rip.sh dvd           — force DVD rip
#   ./helpers/rip.sh bluray        — force Blu-ray rip
#   ./helpers/rip.sh audio         — force audio CD rip
#   ./helpers/rip.sh data          — force data disc → ISO
#
# Set STROPHALOS_COMPOSE to the directory containing your compose.yml.

COMPOSE_DIR="${STROPHALOS_COMPOSE:?Set STROPHALOS_COMPOSE to your compose directory}"
RUN="docker compose -f $COMPOSE_DIR/compose.yml run --rm strophalos"
TYPE="${1:-auto}"
shift 2>/dev/null || true

if [ "$TYPE" = "auto" ]; then
    PROBE=$($RUN probe-disc 2>/dev/null)
    TYPE=$(echo "$PROBE" | grep '^DISC_TYPE=' | cut -d= -f2-)
    LABEL=$(echo "$PROBE" | grep '^DISC_LABEL=' | cut -d= -f2-)
    echo "Detected: $TYPE label='$LABEL'"
fi

case "$TYPE" in
    audio)   $RUN rip-cd "$@" ;;
    dvd)     $RUN rip-dvd ${LABEL:+--label "$LABEL"} "$@" ;;
    bluray)  $RUN rip-video --drive 0 ${LABEL:+--label "$LABEL"} "$@" ;;
    data)    $RUN rip-data ${LABEL:+--label "$LABEL"} "$@" ;;
    *)       echo "Unknown type: $TYPE"; exit 1 ;;
esac
