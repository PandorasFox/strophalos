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
import json
import musicbrainzngs
from whipper.common.config import Config

musicbrainzngs.set_useragent('rip-cd', '1.0', 'local')

conf = Config()
try:
    server = conf._parser.get('musicbrainz', 'server')
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
            rel = releases[0]
            release_detail = musicbrainzngs.get_release_by_id(
                rel['id'], includes=['media', 'discids', 'recordings'])['release']
            disc_total = len(release_detail['medium-list'])
            print('disc_total=' + str(disc_total))
            print('title=' + rel['title'])
            print('artist=' + rel.get('artist-credit-phrase', 'Unknown'))
            print('release_id=' + rel['id'])

            # Build track list for notification
            tracks = []
            for medium in release_detail.get('medium-list', []):
                for track in medium.get('track-list', []):
                    rec = track.get('recording', {})
                    tracks.append({
                        'num': track.get('number', '?'),
                        'title': rec.get('title', '?'),
                        'id': rec.get('id', ''),
                    })
            if tracks:
                print('tracks_json=' + json.dumps(tracks))
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
MB_TITLE=$(echo "$DISC_INFO" | grep '^title=' | cut -d= -f2-)
MB_ARTIST=$(echo "$DISC_INFO" | grep '^artist=' | cut -d= -f2-)
MB_RELEASE_ID=$(echo "$DISC_INFO" | grep '^release_id=' | cut -d= -f2-)
MB_TRACKS_JSON=$(echo "$DISC_INFO" | grep '^tracks_json=' | cut -d= -f2-)

if [ "$DISC_TOTAL" -gt 1 ]; then
    echo "Multi-disc release detected ($DISC_TOTAL discs), using disc number in path"
    TRACK_TPL='%A - %d/Disc %N/%t. %n'
    DISC_TPL='%A - %d/Disc %N/%A - %d'
else
    echo "Single-disc release, using flat layout"
    TRACK_TPL='%A - %d/%t. %n'
    DISC_TPL='%A - %d/%A - %d'
fi

if [ -n "$MB_TITLE" ]; then
    /usr/local/bin/notify.sh "Ripping CD" "${MB_ARTIST:-Unknown} - ${MB_TITLE}"
else
    /usr/local/bin/notify.sh "Ripping CD" "(unknown disc)"
fi

whipper cd -d "$DEVICE" rip \
    -O "$OUTPUT" \
    --track-template "$TRACK_TPL" \
    --disc-template "$DISC_TPL"
RC=$?

# For multi-disc releases, strip disc designations from the top-level directory
# so all discs cluster under a single release directory.
# e.g. "Artist - Album (Disc 1 of 2)/Disc 1/..." → "Artist - Album/Disc 1/..."
if [ "$DISC_TOTAL" -gt 1 ]; then
    for d in "$OUTPUT"/*/; do
        [ -d "$d" ] || continue
        base=$(basename "$d")
        clean=$(echo "$base" | sed -E 's/ *\(([Dd]isc|CD) [0-9]+( of [0-9]+)?\)$//')
        [ "$clean" = "$base" ] && continue
        [ -z "$clean" ] && continue
        target="$OUTPUT/$clean"
        mkdir -p "$target"
        for item in "$d"/*; do
            [ -e "$item" ] && mv "$item" "$target/"
        done
        rmdir "$d" 2>/dev/null
    done
fi

# Notify with release + track info
if [ $RC -eq 0 ] && [ -n "$MB_TITLE" ]; then
    NOTIFY_BODY=$(python3 -c "
import json, sys, os

artist = sys.argv[1]
title = sys.argv[2]
release_id = sys.argv[3]
tracks_json = sys.argv[4] if len(sys.argv) > 4 else ''

mb_url = 'https://musicbrainz.org/release/' + release_id if release_id else ''
lines = []
if mb_url:
    lines.append(f'{artist} - {title}')
    lines.append(mb_url)
else:
    lines.append(f'{artist} - {title}')
lines.append('')

if tracks_json:
    try:
        tracks = json.loads(tracks_json)
        for t in tracks:
            rec_url = 'https://musicbrainz.org/recording/' + t['id'] if t.get('id') else ''
            lines.append(f\"{t['num']}. {t['title']}\")
    except Exception:
        pass

print('\n'.join(lines))
" "$MB_ARTIST" "$MB_TITLE" "$MB_RELEASE_ID" "$MB_TRACKS_JSON" 2>/dev/null)

    /usr/local/bin/notify.sh "CD ripped" "$NOTIFY_BODY"
elif [ $RC -ne 0 ]; then
    /usr/local/bin/notify.sh --error "CD rip failed" "${MB_ARTIST:-Unknown} - ${MB_TITLE:-Unknown} (exit $RC)"
fi

exit $RC
