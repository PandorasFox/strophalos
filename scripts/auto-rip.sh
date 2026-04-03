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

# Scan the disc — tries quick scan first (works for DVD/BD), falls back to
# full scan (needed for UHD AACS2 handshake). Timeout prevents hangs on
# problematic discs.
SCAN_TIMEOUT_QUICK="${SCAN_TIMEOUT_QUICK:-180}"
SCAN_TIMEOUT_FULL="${SCAN_TIMEOUT_FULL:-1200}"

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

_extract_drv() {
    # Extract DRV:0 line from makemkvcon output, sanitize
    printf '%s' "$1" | tr -d '\r' | grep "^DRV:0," | head -1
}

# Run a command with a timeout, killing it properly if it exceeds.
# Usage: run_with_timeout SECONDS command [args...]
# Output goes to $TIMEOUT_OUTPUT. Returns 0 on success, 1 on timeout.
TIMEOUT_OUTPUT=""
run_with_timeout() {
    _timeout_secs="$1"
    shift
    TIMEOUT_OUTPUT=""

    _outfile=$(mktemp)
    setsid sh -c '"$@" > "$0" 2>/dev/null' "$_outfile" "$@" &
    _pid=$!

    _elapsed=0
    while [ "$_elapsed" -lt "$_timeout_secs" ]; do
        # Process gone entirely = done
        if ! kill -0 "$_pid" 2>/dev/null; then
            wait "$_pid" 2>/dev/null
            TIMEOUT_OUTPUT=$(cat "$_outfile")
            rm -f "$_outfile"
            return 0
        fi
        # Zombie = crashed, treat as done
        if grep -q "^State:.*Z" /proc/"$_pid"/status 2>/dev/null; then
            wait "$_pid" 2>/dev/null
            TIMEOUT_OUTPUT=$(cat "$_outfile")
            rm -f "$_outfile"
            return 0
        fi
        sleep 1
        _elapsed=$((_elapsed + 1))
    done

    # Timed out — kill the entire process group (negative PID = group)
    kill -9 -"$_pid" 2>/dev/null
    wait "$_pid" 2>/dev/null
    TIMEOUT_OUTPUT=$(cat "$_outfile")
    rm -f "$_outfile"
    return 1
}

scan_drive() {
    # Quick scan first — works for most discs
    log "quick scan (${SCAN_TIMEOUT_QUICK}s timeout)..." >&2
    if run_with_timeout "$SCAN_TIMEOUT_QUICK" makemkvcon -r info disc:9999; then
        DRV=$(_extract_drv "$TIMEOUT_OUTPUT")
    else
        log "quick scan timed out" >&2
        DRV=""
    fi

    if [ -n "$DRV" ]; then
        DRV_FLAGS=$(printf '%s' "$DRV" | cut -d',' -f4)
        DRV_LABEL=$(printf '%s' "$DRV" | cut -d',' -f6 | tr -d '"')
        if [ -n "$DRV_LABEL" ]; then
            # Got a label — quick scan is definitive
            log "quick scan OK: '$DRV_LABEL' (flags=$DRV_FLAGS)" >&2
            printf '%s' "$DRV"
            return
        fi
        # No label: flags=0 might be audio CD or might be unreadable disc.
        # Fall through to full scan to confirm.
        log "quick scan got flags=$DRV_FLAGS but no label, falling back to full scan..." >&2
    else
        log "quick scan returned nothing, falling back to full scan..." >&2
    fi

    # Reset USB device between scans to clear any leaked handles from dead makemkvcon
    reset_usb_device >&2

    # Full scan with longer timeout (needed for UHD, may hang on some DVDs)
    log "full scan (${SCAN_TIMEOUT_FULL}s timeout)..." >&2
    if run_with_timeout "$SCAN_TIMEOUT_FULL" makemkvcon -r info disc:0; then
        _extract_drv "$TIMEOUT_OUTPUT"
    else
        log "full scan timed out" >&2
    fi
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
for d in /media/archive/tv/rips /media/archive/movies/rips /media/archive/music/rips /media/tv /media/movies /media/music; do
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

    log "disc detected, scanning..."
    DRV=$(scan_drive)
    if [ -z "$DRV" ]; then
        log "scan failed (timeout or unreadable), resetting USB device"
        reset_usb_device
        notify --error "Disc scan failed" "Could not read disc in $DEVICE — remove disc manually"
        DISC_WAS_PRESENT=0
        LAST_DISC=""
        continue
    fi

    # Sanitize — makemkvcon can output \r, \n, or multiple lines
    DRV=$(printf '%s' "$DRV" | tr -d '\r\n' | head -1)
    DRV_FLAGS=$(printf '%s' "$DRV" | cut -d',' -f4)
    DRV_LABEL=$(printf '%s' "$DRV" | cut -d',' -f6 | tr -d '"')

    # Audio CDs have flags=0 and no label — that's expected, route to whipper.
    # Video discs with no label after a full scan are genuinely unreadable.
    if [ -z "$DRV_LABEL" ] && [ "$DRV_FLAGS" -ne 0 ]; then
        log "video disc scan returned no label, skipping"
        continue
    fi

    log "disc ready: '$DRV_LABEL' (flags=$DRV_FLAGS)"

    if [ "$DRV_FLAGS" -eq 0 ]; then
        log "audio disc — handing off to whipper"
        as_user /usr/local/bin/rip-cd.sh
        RC=$?
        # rip-cd.sh handles its own notifications (start + completion with MB info)
    else
        log "video disc — running smart rip"
        notify "Ripping disc" "$DRV_LABEL"
        # Stream rip output to logs in real-time, tee to temp file for parsing
        RIP_LOG=$(mktemp)
        as_user python3 /usr/local/bin/rip-video.py --drive 0 --label "$DRV_LABEL" 2>&1 | tee "$RIP_LOG"; RC=$?
        # Check if rip-video.py actually succeeded (tee always returns 0)
        if grep -q '^STROPHALOS_OUTPUT_DIR=' "$RIP_LOG"; then
            RC=0
        fi

        # Extract metadata from captured output
        OUTPUT_DIR=$(grep '^STROPHALOS_OUTPUT_DIR=' "$RIP_LOG" | cut -d= -f2-)
        OUTPUT_DIR="${OUTPUT_DIR:-/media/archive/movies/rips/unknown/$DRV_LABEL}"
        DISC_TYPE=$(grep '^STROPHALOS_DISC_TYPE=' "$RIP_LOG" | cut -d= -f2-)

        TITLE_COUNT=$(grep '^STROPHALOS_TITLE_COUNT=' "$RIP_LOG" | cut -d= -f2-)

        if [ $RC -eq 0 ]; then
            MEDIA_TYPE=$(grep '^STROPHALOS_MEDIA_TYPE=' "$RIP_LOG" | cut -d= -f2-)
            CONTENT_LABEL="movie"
            [ "$DISC_TYPE" = "tv" ] && CONTENT_LABEL="TV"
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
                    as_user python3 /usr/local/bin/identify-episodes.py \
                        --dir "$OUTPUT_DIR" --label "$DRV_LABEL" \
                        --library /media \
                        >> "$IDENTIFY_LOG" 2>&1 &
                    ;;
                music)
                    log "Audio BD — starting music identification (background)"
                    as_user python3 /usr/local/bin/identify-music.py \
                        --dir "$OUTPUT_DIR" --label "$DRV_LABEL" \
                        --library /media \
                        >> "$IDENTIFY_LOG" 2>&1 &
                    ;;
                *)
                    log "Movie disc — starting identification (background)"
                    as_user python3 /usr/local/bin/identify-movie.py \
                        --dir "$OUTPUT_DIR" --label "$DRV_LABEL" \
                        --library /media \
                        >> "$IDENTIFY_LOG" 2>&1 &
                    ;;
            esac
        fi

        STATUS=$([ $RC -eq 0 ] && echo "SUCCESS" || echo "FAILURE")
        run_hook disc_rip_terminated.sh 0 "$DRV_LABEL" "$OUTPUT_DIR" "$STATUS"
        rm -f "$RIP_LOG"
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
