#!/bin/sh
# Validate API credentials for all configured services.
#
# Usage: ./helpers/check-apis.sh
#
# Set STROPHALOS_COMPOSE to the directory containing your compose.yml.

COMPOSE_DIR="${STROPHALOS_COMPOSE:?Set STROPHALOS_COMPOSE to your compose directory}"
RUN="docker compose -f $COMPOSE_DIR/compose.yml run --rm strophalos"

echo "=== TMDb ==="
$RUN python3 -c "
import os
from strophalos.core.http import get_json
key = os.environ.get('TMDB_API_KEY', '')
if not key:
    print('  NOT CONFIGURED (TMDB_API_KEY unset)')
else:
    data = get_json(f'https://api.themoviedb.org/3/configuration?api_key={key}')
    print('  OK' if data else '  FAILED')
"

echo "=== MusicBrainz ==="
$RUN python3 -c "
import os
from strophalos.core.http import get_json
server = os.environ.get('MB_SERVER', 'mb-web:5000')
data = get_json(f'http://{server}/ws/2/release/?query=test&fmt=json&limit=1',
                headers={'User-Agent': 'strophalos/1.0'}, timeout=5)
print('  OK' if data else '  FAILED (is MB_SERVER reachable?)')
"

echo "=== OpenSubtitles ==="
$RUN python3 -c "
from strophalos.backends.opensubtitles import _load_config, _token_valid
config = _load_config()
if config is None:
    print('  NOT CONFIGURED (need OPENSUBTITLES_API_KEY + run setup-opensubtitles)')
elif _token_valid(config):
    print('  OK')
else:
    print('  TOKEN EXPIRED (re-run setup-opensubtitles)')
"

echo "=== AniDB ==="
$RUN python3 -c "
from strophalos.backends.anidb import _load_config
config = _load_config()
if config is None:
    print('  NOT CONFIGURED (create /config/anidb.json with username/password)')
else:
    print('  CONFIGURED (credentials present, UDP auth tested on first use)')
"

echo "=== Notifications ==="
$RUN python3 -c "
import os
url = os.environ.get('NOTIFY_URL', '')
print('  OK — ' + url[:30] + '...' if url else '  NOT CONFIGURED (NOTIFY_URL unset)')
"
