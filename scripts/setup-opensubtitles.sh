#!/bin/sh
# One-time setup for OpenSubtitles API v2 authentication.
# Run via: docker exec -it strophalos setup-opensubtitles.sh
#
# Requires OPENSUBTITLES_API_KEY to be set in the container environment.
# Prompts for OpenSubtitles username and password, authenticates against the
# API, and stores the JWT token in /config/opensubtitles.json for use by
# identify-episodes.py.

set -eu

CONFIG_FILE="/config/opensubtitles.json"
API_BASE="https://api.opensubtitles.com/api/v1"

die() { echo "ERROR: $*" >&2; exit 1; }

# Check for API key
API_KEY="${OPENSUBTITLES_API_KEY:-}"
if [ -z "$API_KEY" ]; then
    die "OPENSUBTITLES_API_KEY is not set. Add it to your compose environment and recreate the container."
fi

# Prompt for credentials
printf "OpenSubtitles username: "
read -r USERNAME
printf "OpenSubtitles password: "
# Disable echo for password input
stty -echo 2>/dev/null || true
read -r PASSWORD
stty echo 2>/dev/null || true
echo

if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
    die "Username and password are required."
fi

echo "Authenticating with OpenSubtitles..."

# Build JSON body — use python3 to safely escape strings
BODY=$(python3 -c "
import json, sys
print(json.dumps({'username': sys.argv[1], 'password': sys.argv[2]}))
" "$USERNAME" "$PASSWORD")

# POST to login endpoint
RESPONSE=$(python3 -c "
import json, sys, urllib.request

api_key = sys.argv[1]
body = sys.argv[2].encode('utf-8')

req = urllib.request.Request(
    '${API_BASE}/login',
    data=body,
    headers={
        'Api-Key': api_key,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': 'strophalos v1.0',
    },
    method='POST',
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        print(json.dumps(data))
except urllib.error.HTTPError as e:
    error_body = e.read().decode('utf-8', errors='replace')
    print(json.dumps({'error': f'HTTP {e.code}: {error_body}'}), file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(json.dumps({'error': str(e)}), file=sys.stderr)
    sys.exit(1)
" "$API_KEY" "$BODY") || die "Login request failed. Check your credentials and API key."

# Extract token from response
python3 -c "
import json, sys
from datetime import datetime, timedelta, timezone

resp = json.loads(sys.argv[1])
api_key = sys.argv[2]

token = resp.get('token')
if not token:
    print('ERROR: No token in response: ' + json.dumps(resp), file=sys.stderr)
    sys.exit(1)

# OpenSubtitles JWT tokens are valid for 24 hours
expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

config = {
    'token': token,
    'expires': expires,
    'api_key': api_key,
    'base_url': resp.get('base_url', ''),
}

with open('${CONFIG_FILE}', 'w') as f:
    json.dump(config, f, indent=2)

print('Token stored in ${CONFIG_FILE}')
print(f'  Expires: {expires}')
if resp.get('base_url'):
    print(f'  Base URL: {resp[\"base_url\"]}')
print('OpenSubtitles setup complete.')
" "$RESPONSE" "$API_KEY" || die "Failed to parse login response."
