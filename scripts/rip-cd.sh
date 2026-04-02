#!/bin/sh
# rip-cd.sh — wrapper around whipper that auto-detects multi-disc releases
# and adjusts the output template accordingly.
#
# Usage: rip-cd.sh [-d /dev/sr1] [-o /output-cd]

export HOME=/config

DEVICE="/dev/sr1"
OUTPUT="/output-cd"

while getopts "d:o:" opt; do
    case "$opt" in
        d) DEVICE="$OPTARG" ;;
        o) OUTPUT="$OPTARG" ;;
        *) echo "Usage: rip-cd.sh [-d device] [-o output_dir]"; exit 1 ;;
    esac
done

# Query disc info via whipper/python to get disc count from MB
DISC_INFO=$(python3 -c "
import discid
import musicbrainzngs
from whipper.common.config import Config

musicbrainzngs.set_useragent('rip-cd', '1.0', 'local')

conf = Config()
try:
    server = conf._parser.get('musicbrainz', 'server')
    # musicbrainzngs wants just host:port, no scheme
    server = server.replace('http://', '').replace('https://', '')
    musicbrainzngs.set_hostname(server)
except Exception:
    pass

disc = discid.read('$DEVICE')
disc_id = disc.id
print('disc_id=' + disc_id)

try:
    result = musicbrainzngs.get_releases_by_discid(
        disc_id, includes=['artists', 'recordings', 'release-groups'])
    if result.get('disc'):
        releases = result['disc']['release-list']
        if releases:
            # pick first release to check disc count
            rel = releases[0]
            release_detail = musicbrainzngs.get_release_by_id(
                rel['id'], includes=['media', 'discids'])['release']
            disc_total = len(release_detail['medium-list'])
            print('disc_total=' + str(disc_total))
            print('title=' + rel['title'])
            print('artist=' + rel.get('artist-credit-phrase', 'Unknown'))
        else:
            print('disc_total=1')
    else:
        print('disc_total=1')
except Exception as e:
    import sys
    print('error=' + str(e), file=sys.stderr)
    print('disc_total=1')
" 2>&1)

echo "$DISC_INFO"

DISC_TOTAL=$(echo "$DISC_INFO" | grep '^disc_total=' | cut -d= -f2)
DISC_TOTAL="${DISC_TOTAL:-1}"

if [ "$DISC_TOTAL" -gt 1 ]; then
    echo "Multi-disc release detected ($DISC_TOTAL discs), using disc number in path"
    TRACK_TPL='%A - %d/Disc %N/%t. %n'
    DISC_TPL='%A - %d/Disc %N/%A - %d'
else
    echo "Single-disc release, using flat layout"
    TRACK_TPL='%A - %d/%t. %n'
    DISC_TPL='%A - %d/%A - %d'
fi

exec whipper cd -d "$DEVICE" rip \
    -O "$OUTPUT" \
    --track-template "$TRACK_TPL" \
    --disc-template "$DISC_TPL"
