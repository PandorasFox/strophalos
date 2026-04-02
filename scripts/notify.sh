#!/bin/sh
# notify.sh — send notifications via apprise (Telegram, Pushbullet, ntfy, etc.)
#
# Usage:
#   notify.sh "Title" "Body"
#   notify.sh --error "Title" "Body"    (sends as failure/high priority)
#
# Requires NOTIFY_URL env var, e.g.:
#   tgram://bot_token/chat_id
#   pbul://api_key
#   ntfy://topic
#
# Does nothing if NOTIFY_URL is unset — safe to call unconditionally.

NOTIFY_URL="${NOTIFY_URL:-}"
[ -z "$NOTIFY_URL" ] && exit 0

TYPE="info"
if [ "$1" = "--error" ]; then
    TYPE="failure"
    shift
fi

TITLE="${1:-strophalos}"
BODY="${2:-}"

python3 -c "
import sys
try:
    import apprise
    a = apprise.Apprise()
    a.add(sys.argv[1])
    a.notify(title=sys.argv[2], body=sys.argv[3], notify_type=sys.argv[4])
except Exception as e:
    print(f'notify: {e}', file=sys.stderr)
" "$NOTIFY_URL" "$TITLE" "$BODY" "$TYPE" 2>/dev/null || true
