#!/bin/sh
# Check current rip status and recent identification logs.
#
# Usage: ./helpers/rip-status.sh
#
# Set STROPHALOS_COMPOSE to the directory containing your compose.yml.

COMPOSE_DIR="${STROPHALOS_COMPOSE:?Set STROPHALOS_COMPOSE to your compose directory}"
SERVICE="strophalos"

echo "=== Service Status ==="
docker compose -f "$COMPOSE_DIR/compose.yml" ps "$SERVICE" 2>/dev/null || echo "Service not running"

echo ""
echo "=== Recent Logs (last 20 lines) ==="
docker compose -f "$COMPOSE_DIR/compose.yml" logs --tail 20 "$SERVICE" 2>&1

echo ""
echo "=== Recent Identification Logs ==="
docker compose -f "$COMPOSE_DIR/compose.yml" run --rm "$SERVICE" sh -c '
ls -t /config/logs/identify-*.log 2>/dev/null | head -3 | while read f; do
    echo "--- $(basename "$f") ---"
    tail -5 "$f"
    echo ""
done
' 2>/dev/null || echo "  No identification logs found"
