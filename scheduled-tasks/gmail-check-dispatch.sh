#!/usr/bin/env bash
# gmail-check-dispatch.sh
# Layer 1 pre-check for gmail-email-pipeline.
#
# Rate-limits full pipeline runs to at most once every 6 hours by tracking the
# last-check timestamp. Within that window, polls Gmail for any unread inbox
# messages. If none found, logs a no-op and exits 0 (no LLM spawned).
# If unread messages exist, calls dispatch-job.sh to trigger the full pipeline.
#
# Flow:
#   last check < 6h ago               → exit 0 (rate limit, no query)
#   last check >= 6h ago, no unread   → log no-op → exit 0
#   last check >= 6h ago, unread found → dispatch-job.sh gmail-email-pipeline
#
# State file: ~/lobster-workspace/data/gmail-last-check.txt
#   Contains an ISO 8601 timestamp of the last time the pipeline was dispatched.
#
# Note: The 6-hour rate limit applies to LLM dispatch only. The cron entry fires
# this script every 30 minutes, but the script gates on the state file timestamp
# so the heavy pipeline runs at most 4 times per day.

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

JOB_NAME="gmail-email-pipeline"
REPO_DIR="${REPO_DIR:-$HOME/lobster}"
WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
STATE_FILE="$WORKSPACE/data/gmail-last-check.txt"
LOG_PREFIX="[gmail-check]"
RATE_LIMIT_HOURS=6

# --- Load env ---
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
for _env_file in "$CONFIG_DIR/config.env" "$CONFIG_DIR/global.env"; do
    if [ -f "$_env_file" ]; then
        set -a
        # shellcheck source=/dev/null
        source "$_env_file"
        set +a
    fi
done
unset _env_file

# --- Check rate limit ---
NOW_EPOCH=$(date +%s)
if [ -f "$STATE_FILE" ]; then
    LAST_CHECK=$(cat "$STATE_FILE" | tr -d '[:space:]')
    LAST_EPOCH=$(python3 -c "
import sys
from datetime import datetime, timezone
try:
    ts = datetime.fromisoformat('$LAST_CHECK'.replace('Z', '+00:00'))
    print(int(ts.timestamp()))
except Exception:
    print(0)
" 2>/dev/null || echo "0")
    ELAPSED_SECONDS=$(( NOW_EPOCH - LAST_EPOCH ))
    RATE_LIMIT_SECONDS=$(( RATE_LIMIT_HOURS * 3600 ))
    if [ "$ELAPSED_SECONDS" -lt "$RATE_LIMIT_SECONDS" ]; then
        REMAINING=$(( (RATE_LIMIT_SECONDS - ELAPSED_SECONDS) / 60 ))
        echo "$LOG_PREFIX Rate limit: last dispatched $ELAPSED_SECONDS seconds ago — next eligible in ~${REMAINING}m"
        exit 0
    fi
fi

echo "$LOG_PREFIX Rate limit cleared — checking Gmail for unread messages"

# --- Query Gmail for unread inbox messages ---
# Uses gws CLI (Google Workspace CLI). If gws is unavailable, dispatch anyway
# so the job can report the missing dependency.
if ! command -v gws &>/dev/null; then
    echo "$LOG_PREFIX gws not found — dispatching to let job handle missing dependency"
    exec "$REPO_DIR/scheduled-tasks/dispatch-job.sh" "$JOB_NAME"
fi

set +e
UNREAD_COUNT=$(gws gmail users messages list \
    --params '{"userId":"me","q":"is:unread in:inbox","maxResults":"1"}' \
    2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    msgs = data.get('messages', [])
    print(len(msgs))
except (json.JSONDecodeError, Exception):
    sys.exit(2)
" 2>/dev/null)
GWS_EXIT=$?
set -e

if [ "$GWS_EXIT" -ne 0 ]; then
    echo "$LOG_PREFIX Gmail API query failed — dispatching to let job handle it"
    exec "$REPO_DIR/scheduled-tasks/dispatch-job.sh" "$JOB_NAME"
fi

if [ "${UNREAD_COUNT:-0}" -eq 0 ]; then
    echo "$LOG_PREFIX No unread inbox messages — skipping dispatch"
    # Update the last-check timestamp so the rate limit window advances
    date -Iseconds > "$STATE_FILE"
    exit 0
fi

echo "$LOG_PREFIX $UNREAD_COUNT unread message(s) found — dispatching to inbox"
# Update timestamp before dispatch so concurrent runs don't double-dispatch
date -Iseconds > "$STATE_FILE"
exec "$REPO_DIR/scheduled-tasks/dispatch-job.sh" "$JOB_NAME"
