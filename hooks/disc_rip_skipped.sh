#!/bin/sh
# Hook: disc_rip_skipped.sh
# Called by jlesage/makemkv auto-ripper when a disc is skipped.
# Args: $1=drive_id  $2=disc_label  $3=reason
#   reason: ALREADY_PROCESSED | NOT_VIDEO_DISC | SERVICE_FIRST_RUN

DRIVE_ID="$1"
DISC_LABEL="$2"
REASON="$3"

if [ "$REASON" = "NOT_VIDEO_DISC" ]; then
    echo "disc_rip_skipped: audio CD detected ('$DISC_LABEL'), handing off to whipper"
    /usr/local/bin/rip-cd
else
    echo "disc_rip_skipped: '$DISC_LABEL' skipped ($REASON)"
fi
