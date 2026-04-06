#!/bin/sh
# strophalos auto-rip — main container entrypoint
# Watches the drive for disc insertion and auto-rips.
#   - Audio CDs   → rip-cd (whipper + MusicBrainz)
#   - DVDs        → rip-dvd (dvdbackup + mkvmerge, no SCSI)
#   - Blu-rays    → rip-video (makemkvcon, SCSI — last resort)
#   - Data discs  → rip-data (dd → .iso)
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

USB_PORT="${USB_PORT:-2-5}"

reset_usb_device() {
    UNBIND="/sys/bus/usb/drivers/usb/unbind"
    BIND="/sys/bus/usb/drivers/usb/bind"
    if [ ! -w "$UNBIND" ]; then
        log "USB reset: sysfs not writable"
        return
    fi
    log "USB reset: unbinding $USB_PORT..."
    printf '%s' "$USB_PORT" > "$UNBIND" 2>/dev/null
    sleep 2
    log "USB reset: rebinding $USB_PORT..."
    printf '%s' "$USB_PORT" > "$BIND" 2>/dev/null
    sleep 2
    log "USB reset: done"
}

eject_disc() {
    eject "$DEVICE" 2>/dev/null || log "eject failed"
}

as_user() {
    setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
}

run_hook() {
    hook="/config/hooks/$1"
    shift
    if [ -f "$hook" ]; then
        log "hook: $(basename "$hook")"
        sh "$hook" "$@"
    fi
}

# ---------------------------------------------------------------------------
# Rip a video disc (DVD or Blu-ray) and run background identification.
# Expects STROPHALOS_* output from the rip command.
# Usage: rip_video_disc RIP_CMD [args...]
# ---------------------------------------------------------------------------
rip_video_disc() {
    RIP_LOG=$(mktemp)
    as_user "$@" 2>&1 | tee "$RIP_LOG"; RC=$?

    # Check if rip command actually succeeded (tee always returns 0)
    if grep -q '^STROPHALOS_OUTPUT_DIR=' "$RIP_LOG"; then
        RC=0
    fi

    OUTPUT_DIR=$(grep '^STROPHALOS_OUTPUT_DIR=' "$RIP_LOG" | cut -d= -f2-)
    OUTPUT_DIR="${OUTPUT_DIR:-/media/archive/unknown}"
    DISC_TYPE=$(grep '^STROPHALOS_DISC_TYPE=' "$RIP_LOG" | cut -d= -f2-)
    TITLE_COUNT=$(grep '^STROPHALOS_TITLE_COUNT=' "$RIP_LOG" | cut -d= -f2-)
    MEDIA_TYPE=$(grep '^STROPHALOS_MEDIA_TYPE=' "$RIP_LOG" | cut -d= -f2-)

    if [ $RC -eq 0 ]; then
        CONTENT_LABEL="movie"
        [ "$DISC_TYPE" = "tv" ] && CONTENT_LABEL="TV"
        [ "$DISC_TYPE" = "music" ] && CONTENT_LABEL="music"
        notify "Disc ripped" "$DRV_LABEL — ${TITLE_COUNT:-?} title(s), $CONTENT_LABEL ($MEDIA_TYPE)"
    else
        notify --error "Disc rip failed" "$DRV_LABEL (exit $RC)"
    fi

    # Library identification (background — don't block next rip)
    if [ "$RC" -eq 0 ] && [ -n "${TMDB_API_KEY:-}" ]; then
        IDENTIFY_LOG="/config/logs/identify-$(date +%Y%m%d-%H%M%S).log"
        mkdir -p /config/logs
        case "$DISC_TYPE" in
            tv)
                log "TV disc — starting episode identification (background)"
                as_user identify-episodes \
                    --dir "$OUTPUT_DIR" --label "$DRV_LABEL" \
                    --library /media \
                    >> "$IDENTIFY_LOG" 2>&1 &
                ;;
            music)
                log "Audio disc — starting music identification (background)"
                as_user identify-music \
                    --dir "$OUTPUT_DIR" --label "$DRV_LABEL" \
                    --library /media \
                    >> "$IDENTIFY_LOG" 2>&1 &
                ;;
            *)
                log "Movie disc — starting identification (background)"
                as_user identify-movie \
                    --dir "$OUTPUT_DIR" --label "$DRV_LABEL" \
                    --library /media \
                    >> "$IDENTIFY_LOG" 2>&1 &
                ;;
        esac
    fi

    STATUS=$([ $RC -eq 0 ] && echo "SUCCESS" || echo "FAILURE")
    run_hook disc_rip_terminated.sh 0 "$DRV_LABEL" "$OUTPUT_DIR" "$STATUS"
    rm -f "$RIP_LOG"
}

# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

setup-key

mkdir -p /config/hooks /config/.config/whipper /config/.MakeMKV
ln -sf /config /config/.MakeMKV 2>/dev/null || true

chmod 666 /dev/sr* /dev/sg* 2>/dev/null
chown -R "$PUID:$PGID" /config /output-cd 2>/dev/null
for d in /media/archive/tv/rips /media/archive/movies/rips /media/archive/music/rips /media/archive/iso /media/tv /media/movies /media/music; do
    mkdir -p "$d" 2>/dev/null
    chown "$PUID:$PGID" "$d" 2>/dev/null
done

if [ ! -f /config/.config/whipper/whipper.conf ] && [ -f /defaults/whipper.conf ]; then
    cp /defaults/whipper.conf /config/.config/whipper/whipper.conf
    log "seeded whipper config from defaults"
fi

for hook in /defaults/hooks/*; do
    [ -f "$hook" ] || continue
    name=$(basename "$hook")
    if [ ! -f "/config/hooks/$name" ]; then
        cp "$hook" "/config/hooks/$name"
        log "seeded hook: $name"
    fi
done

log "strophalos starting — device=$DEVICE poll=${POLL_INTERVAL}s"

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

TICK=0
while true; do
    sleep "$POLL_INTERVAL"
    TICK=$((TICK + 1))

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

    if [ "$DISC_WAS_PRESENT" -eq 1 ] && [ -n "$LAST_DISC" ]; then
        continue
    fi

    DISC_WAS_PRESENT=1

    # --- Probe disc type (no SCSI, no makemkvcon) ---
    log "disc detected, probing..."
    PROBE_OUTPUT=$(probe-disc 2>&1)
    PROBE_TYPE=$(echo "$PROBE_OUTPUT" | grep '^DISC_TYPE=' | cut -d= -f2-)
    DRV_LABEL=$(echo "$PROBE_OUTPUT" | grep '^DISC_LABEL=' | cut -d= -f2-)

    log "probe: type=$PROBE_TYPE label='$DRV_LABEL'"

    case "$PROBE_TYPE" in
        audio)
            log "audio CD — handing off to whipper"
            as_user rip-cd
            ;;

        dvd)
            log "DVD — ripping via dvdbackup (no SCSI)"
            notify "Ripping DVD" "$DRV_LABEL"
            rip_video_disc rip-dvd --label "$DRV_LABEL"
            ;;

        bluray)
            log "Blu-ray — ripping via makemkvcon (SCSI)"
            notify "Ripping Blu-ray" "$DRV_LABEL"
            rip_video_disc rip-video --drive 0 --label "$DRV_LABEL"
            ;;

        audio+data)
            log "audio+data disc — ripping ISO + audio tracks"
            notify "Ripping audio+data disc" "$DRV_LABEL"
            as_user rip-data --label "$DRV_LABEL"
            as_user rip-cd
            ;;

        data)
            log "data disc — ripping to ISO"
            as_user rip-data --label "$DRV_LABEL"
            ;;

        *)
            # Unknown — try makemkvcon as last resort (might be encrypted BD)
            log "unknown disc type, trying makemkvcon as fallback..."
            notify "Ripping disc" "${DRV_LABEL:-unknown}"
            rip_video_disc rip-video --drive 0 --label "${DRV_LABEL:-unknown}"
            ;;
    esac

    LAST_DISC="$PROBE_TYPE:$DRV_LABEL"

    if [ "$EJECT_ON_COMPLETE" = "1" ]; then
        log "ejecting"
        eject_disc
        LAST_DISC=""
        DISC_WAS_PRESENT=0
    fi

    log "ready for next disc"
done
