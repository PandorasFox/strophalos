#!/bin/sh
# strophalos auto-rip — main container entrypoint
# Watches the drive for disc insertion and auto-rips using smart title selection.
#   - Video discs → rip-video (smart title selection)
#   - Audio CDs   → rip-cd (whipper + MusicBrainz)
#   - Ejects on completion

set -u

export HOME=/config
export LD_LIBRARY_PATH="/opt/makemkv/lib"

DEVICE="${DEVICE:-/dev/sr1}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
EJECT_ON_COMPLETE="${EJECT_ON_COMPLETE:-1}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
LAST_DISC=""
DISC_WAS_PRESENT=0

log() {
    echo "[strophalos] $(date '+%H:%M:%S') $*"
}

# Lightweight disc presence check via ioctl — no drive activity.
check_disc_present() {
    STATUS=$(python3 -c "
import fcntl, os, sys
try:
    fd = os.open(os.environ.get('DEVICE', '/dev/sr1'), os.O_RDONLY | os.O_NONBLOCK)
    status = fcntl.ioctl(fd, 0x5326)
    os.close(fd)
    print(status)
except Exception as e:
    print(-1)
" 2>/dev/null)
    [ "$STATUS" = "4" ]
}

# Full disc scan via makemkvcon — only called when disc is confirmed present.
# Uses disc:0 (full scan) instead of disc:9999 (quick list) because UHD discs
# need the full AACS2 handshake to be properly identified.
scan_drive() {
    makemkvcon -r info disc:0 2>/dev/null | grep "^DRV:0,"
}

eject_disc() {
    eject "$DEVICE" 2>/dev/null || log "eject failed"
}

as_user() {
    setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
}

notify() {
    /usr/local/bin/notify.sh "$@"
}

run_hook() {
    hook="/config/hooks/$1"
    shift
    if [ -f "$hook" ]; then
        log "hook: $(basename "$hook")"
        sh "$hook" "$@"
    fi
}

# Set up MakeMKV key
/usr/local/bin/setup-key.sh

# Ensure config dirs exist
mkdir -p /config/hooks /config/.config/whipper /config/.MakeMKV
ln -sf /config /config/.MakeMKV 2>/dev/null || true

# Open device access for non-root rip processes and own the output dirs
chmod 666 /dev/sr* /dev/sg* 2>/dev/null
chown -R "$PUID:$PGID" /config /output-cd 2>/dev/null
# Ensure archive + library dirs exist (don't recursive chown /media)
for d in /media/archive/tv/rips /media/archive/movies/rips /media/tv /media/movies; do
    mkdir -p "$d" 2>/dev/null
    chown "$PUID:$PGID" "$d" 2>/dev/null
done

# Seed whipper config if missing
if [ ! -f /config/.config/whipper/whipper.conf ] && [ -f /defaults/whipper.conf ]; then
    cp /defaults/whipper.conf /config/.config/whipper/whipper.conf
    log "seeded whipper config from defaults"
fi

# Seed hooks if missing
for hook in /defaults/hooks/*; do
    [ -f "$hook" ] || continue
    name=$(basename "$hook")
    if [ ! -f "/config/hooks/$name" ]; then
        cp "$hook" "/config/hooks/$name"
        log "seeded hook: $name"
    fi
done

log "strophalos starting — device=$DEVICE poll=${POLL_INTERVAL}s"

TICK=0
while true; do
    sleep "$POLL_INTERVAL"
    TICK=$((TICK + 1))

    # Heartbeat every ~60s so we know the loop is alive
    if [ $((TICK % 12)) -eq 0 ]; then
        HB_STATUS=$(python3 -c "
import fcntl, os
fd = os.open(os.environ.get('DEVICE', '/dev/sr1'), os.O_RDONLY | os.O_NONBLOCK)
s = fcntl.ioctl(fd, 0x5326)
os.close(fd)
print(s)
" 2>/dev/null || echo "err")
        log "heartbeat (tick=$TICK, disc_present=$DISC_WAS_PRESENT, ioctl=$HB_STATUS)"
    fi

    if ! check_disc_present; then
        if [ "$DISC_WAS_PRESENT" -eq 1 ]; then
            log "disc removed"
            DISC_WAS_PRESENT=0
            LAST_DISC=""
        fi
        continue
    fi

    # Disc is present — but have we already handled it?
    if [ "$DISC_WAS_PRESENT" -eq 1 ] && [ -n "$LAST_DISC" ]; then
        continue
    fi

    DISC_WAS_PRESENT=1

    log "disc detected, running full scan (this can take several minutes for UHD)..."
    DRV=$(scan_drive)
    if [ -z "$DRV" ]; then
        log "scan returned no data, will retry next cycle"
        DISC_WAS_PRESENT=0
        continue
    fi

    DRV_FLAGS=$(echo "$DRV" | cut -d',' -f4)
    DRV_LABEL=$(echo "$DRV" | cut -d',' -f6 | tr -d '"')

    # Audio CDs have flags=0 and no label — that's expected, route to whipper.
    # Video discs with no label after a full scan are genuinely unreadable.
    if [ -z "$DRV_LABEL" ] && [ "$DRV_FLAGS" -ne 0 ]; then
        log "video disc scan returned no label, skipping"
        continue
    fi

    log "disc ready: '$DRV_LABEL' (flags=$DRV_FLAGS)"

    if [ "$DRV_FLAGS" -eq 0 ]; then
        log "audio disc — handing off to whipper"
        notify "Ripping CD" "$DRV_LABEL"
        as_user /usr/local/bin/rip-cd.sh
        RC=$?
        # rip-cd.sh sends its own detailed notification with MusicBrainz info
    else
        log "video disc — running smart rip"
        notify "Ripping disc" "$DRV_LABEL"
        RIP_OUTPUT=$(as_user python3 /usr/local/bin/rip-video.py --drive 0 --label "$DRV_LABEL" 2>&1)
        RC=$?
        echo "$RIP_OUTPUT"

        # Extract actual output dir from rip-video.py
        OUTPUT_DIR=$(echo "$RIP_OUTPUT" | grep '^STROPHALOS_OUTPUT_DIR=' | cut -d= -f2-)
        OUTPUT_DIR="${OUTPUT_DIR:-/media/archive/movies/rips/unknown/$DRV_LABEL}"
        DISC_TYPE=$(echo "$RIP_OUTPUT" | grep '^STROPHALOS_DISC_TYPE=' | cut -d= -f2-)

        TITLE_COUNT=$(echo "$RIP_OUTPUT" | grep '^STROPHALOS_TITLE_COUNT=' | cut -d= -f2-)

        if [ $RC -eq 0 ]; then
            MEDIA_TYPE=$(echo "$RIP_OUTPUT" | grep '^STROPHALOS_MEDIA_TYPE=' | cut -d= -f2-)
            CONTENT_LABEL="movie"
            [ "$DISC_TYPE" = "tv" ] && CONTENT_LABEL="TV"
            notify "Disc ripped" "$DRV_LABEL — ${TITLE_COUNT:-?} title(s), $CONTENT_LABEL ($MEDIA_TYPE)"
        else
            notify --error "Disc rip failed" "$DRV_LABEL (exit $RC)"
        fi

        # Episode identification for TV discs (background — don't block next rip)
        if [ "$RC" -eq 0 ] && [ "$DISC_TYPE" = "tv" ] && [ -n "${TMDB_API_KEY:-}" ]; then
            log "TV disc — starting episode identification (background)"
            IDENTIFY_LOG="/config/logs/identify-$(date +%Y%m%d-%H%M%S).log"
            mkdir -p /config/logs
            as_user python3 /usr/local/bin/identify-episodes.py \
                --dir "$OUTPUT_DIR" --label "$DRV_LABEL" \
                --library /media \
                >> "$IDENTIFY_LOG" 2>&1 &
        fi

        STATUS=$([ $RC -eq 0 ] && echo "SUCCESS" || echo "FAILURE")
        run_hook disc_rip_terminated.sh 0 "$DRV_LABEL" "$OUTPUT_DIR" "$STATUS"
    fi

    LAST_DISC="$DRV"

    if [ "$EJECT_ON_COMPLETE" = "1" ]; then
        log "ejecting"
        eject_disc
        LAST_DISC=""
        DISC_WAS_PRESENT=0
    fi

    log "ready for next disc"
done
