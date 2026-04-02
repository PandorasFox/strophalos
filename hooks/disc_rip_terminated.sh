#!/bin/sh
# Hook: disc_rip_terminated.sh
# Called by jlesage/makemkv auto-ripper after a disc finishes.
# Args: $1=drive_id  $2=disc_label  $3=output_dir  $4=status (SUCCESS|FAILURE)
#
# Sorts completed rips into subdirectories by disc type:
#   /output/dvd/LABEL/
#   /output/bluray/LABEL/
#   /output/uhd/LABEL/

DRIVE_ID="$1"
DISC_LABEL="$2"
OUTPUT_DIR="$3"
STATUS="$4"

if [ "$STATUS" != "SUCCESS" ]; then
    echo "disc_rip_terminated: rip failed for '$DISC_LABEL', skipping sort"
    exit 0
fi

if [ ! -d "$OUTPUT_DIR" ]; then
    echo "disc_rip_terminated: output dir '$OUTPUT_DIR' not found, skipping"
    exit 0
fi

# Get physical media type from the drive's MMC profile via sg_get_config.
# Profiles: DVD-ROM, DVD-R, DVD+R, BD-ROM, BD-R, BD-RE, etc.
# UHD discs report as BD-ROM but use AACS2; distinguish by output size.
PROFILE=$(sg_get_config --current /dev/sr1 2>/dev/null | grep 'Current profile:' | sed 's/.*Current profile: //')

case "$PROFILE" in
    BD-ROM|BD-R|BD-RE)
        SIZE_BYTES=$(du -sb "$OUTPUT_DIR" 2>/dev/null | cut -f1)
        SIZE_GB=$((SIZE_BYTES / 1073741824))
        if [ "$SIZE_GB" -ge 50 ]; then
            DISC_TYPE="uhd"
        else
            DISC_TYPE="bluray"
        fi
        ;;
    DVD*)
        DISC_TYPE="dvd"
        ;;
    *)
        DISC_TYPE="unknown"
        ;;
esac

echo "disc_rip_terminated: profile='$PROFILE' -> $DISC_TYPE"

DEST="/output/$DISC_TYPE"
mkdir -p "$DEST"

DIRNAME=$(basename "$OUTPUT_DIR")
if [ -d "$DEST/$DIRNAME" ]; then
    DIRNAME="${DIRNAME}-$(date +%Y%m%d%H%M%S)"
fi

mv "$OUTPUT_DIR" "$DEST/$DIRNAME"
echo "disc_rip_terminated: sorted '$DISC_LABEL' -> $DISC_TYPE/$DIRNAME"
