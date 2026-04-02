#!/bin/sh
# Hook: disc_rip_terminated.sh
# Called after a video disc rip completes.
# Args: $1=drive_id  $2=disc_label  $3=output_dir  $4=status (SUCCESS|FAILURE)
#
# Media type sorting (dvd/bd/uhd) is handled by rip-video.py at rip time.
# This hook is for custom post-processing — notifications, transcoding, etc.

DRIVE_ID="$1"
DISC_LABEL="$2"
OUTPUT_DIR="$3"
STATUS="$4"

echo "disc_rip_terminated: label='$DISC_LABEL' status=$STATUS dir='$OUTPUT_DIR'"
