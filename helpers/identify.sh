#!/bin/sh
# Re-run episode/movie/music identification on an existing rip.
#
# Usage:
#   ./helpers/identify.sh episodes --dir /media/archive/tv/rips/bd/SHOW/disc1 --label SHOW
#   ./helpers/identify.sh movie --dir /media/archive/movies/rips/bd/TITLE/disc1 --label TITLE
#   ./helpers/identify.sh music --dir /media/archive/music/rips/bd/ALBUM/disc1 --label ALBUM
#
# Add --dry-run to preview without linking.
#
# Set STROPHALOS_COMPOSE to the directory containing your compose.yml.

COMPOSE_DIR="${STROPHALOS_COMPOSE:?Set STROPHALOS_COMPOSE to your compose directory}"
TYPE="${1:?Usage: identify.sh <episodes|movie|music> [args...]}"
shift

case "$TYPE" in
    episodes) docker compose -f "$COMPOSE_DIR/compose.yml" run --rm strophalos identify-episodes "$@" ;;
    movie)    docker compose -f "$COMPOSE_DIR/compose.yml" run --rm strophalos identify-movie "$@" ;;
    music)    docker compose -f "$COMPOSE_DIR/compose.yml" run --rm strophalos identify-music "$@" ;;
    *)        echo "Unknown type: $TYPE (use episodes, movie, or music)"; exit 1 ;;
esac
