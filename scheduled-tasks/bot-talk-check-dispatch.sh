#!/bin/bash
# Bot-Talk Pre-Check Dispatcher
#
# Performs a lightweight HTTP check against the bot-talk API before dispatching
# the bot-talk-poller job. If there are no new messages since last_message_ts,
# exits immediately without writing to the inbox (no LLM subagent spawned).
# If new messages exist, delegates to dispatch-job.sh as normal.
#
# Usage: bot-talk-check-dispatch.sh bot-talk-poller
#
# Flow:
#   new messages  → dispatch-job.sh bot-talk-poller → inbox write → LLM
#   no new msgs   → log no-op → exit 0 (no inbox write)

set -e

export PATH="$HOME/.local/bin:$PATH"

JOB_NAME="${1:-bot-talk-poller}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load env files (same pattern as dispatch-job.sh)
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
for _env_file in "$CONFIG_DIR/config.env" "$CONFIG_DIR/global.env"; do
    if [ -f "$_env_file" ]; then
        # shellcheck source=/dev/null
        set -a
        source "$_env_file"
        set +a
    fi
done
unset _env_file

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOG_DIR="$WORKSPACE/scheduled-jobs/logs"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
START_ISO=$(date -Iseconds)

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${JOB_NAME}-precheck-${TIMESTAMP}.log"

# --- Read state file ---
STATE_FILE="$WORKSPACE/data/bot-talk-state.json"
LAST_TS=""
if [ -f "$STATE_FILE" ]; then
    LAST_TS=$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        d = json.load(f)
    print(d.get('last_message_ts', ''))
except Exception:
    pass
" 2>/dev/null || true)
fi

# Fallback: legacy last-seen file
if [ -z "$LAST_TS" ] && [ -f "$WORKSPACE/data/bot-talk-last-seen.txt" ]; then
    LAST_TS=$(python3 -c "
import sys
val = open('$WORKSPACE/data/bot-talk-last-seen.txt').read().strip()
print(val)
" 2>/dev/null || true)
fi

# --- Read token ---
TOKEN_FILE="$WORKSPACE/data/bot-talk-token.txt"
if [ ! -f "$TOKEN_FILE" ]; then
    echo "[$START_ISO] ERROR: bot-talk token file missing: $TOKEN_FILE" | tee "$LOG_FILE"
    # Fall through to dispatch so the job can report the failure properly
    exec "$SCRIPT_DIR/dispatch-job.sh" "$JOB_NAME"
fi
BOT_TOKEN=$(python3 -c "print(open('$TOKEN_FILE').read().strip())" 2>/dev/null)

# --- Determine API URL ---
BOT_TALK_API_URL="${BOT_TALK_API_URL:-http://46.224.41.108:4242}"

# --- Check for new messages ---
# A single HTTP request with ?since=<last_ts> returns only messages after that point.
# If the array is empty (or the field count is 0), nothing is new.
HAS_NEW_MESSAGES=0
API_ERROR=0

ENCODED_TS=""
if [ -n "$LAST_TS" ]; then
    # URL-encode the timestamp for the query string
    ENCODED_TS=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "$LAST_TS" 2>/dev/null || echo "")
    CHECK_URL="$BOT_TALK_API_URL/messages?since=${ENCODED_TS}"
else
    # No known last timestamp — treat as new to be safe
    HAS_NEW_MESSAGES=1
fi

if [ "$HAS_NEW_MESSAGES" -eq 0 ] && [ -n "$ENCODED_TS" ]; then
    RESPONSE=$(curl -sf --max-time 10 \
        -H "X-Bot-Token: $BOT_TOKEN" \
        "$CHECK_URL" 2>/dev/null) || API_ERROR=$?

    if [ "$API_ERROR" -ne 0 ]; then
        echo "[$START_ISO] WARNING: bot-talk API unreachable (curl exit $API_ERROR) — dispatching to let job handle outage logic" | tee "$LOG_FILE"
        exec "$SCRIPT_DIR/dispatch-job.sh" "$JOB_NAME"
    fi

    # Count messages in the response array.
    # On JSON parse failure, fall through to dispatch (fail-safe: err on the side of running the job).
    # set -e is disabled around this call so we can inspect the exit code ourselves.
    set +e
    MSG_COUNT=$(python3 -c "
import json, sys
data_str = sys.argv[1]
try:
    data = json.loads(data_str)
except json.JSONDecodeError:
    # Invalid JSON from API — signal parse failure so caller falls through to dispatch
    sys.exit(2)
# API returns either a list directly or {\"messages\": [...]}
if isinstance(data, list):
    msgs = data
else:
    msgs = data.get('messages', [])
print(len(msgs))
" "$RESPONSE" 2>/dev/null)
    PARSE_EXIT=$?
    set -e

    if [ "$PARSE_EXIT" -ne 0 ]; then
        echo "[$START_ISO] WARNING: bot-talk API returned invalid JSON — dispatching to let job handle it" | tee "$LOG_FILE"
        exec "$SCRIPT_DIR/dispatch-job.sh" "$JOB_NAME"
    fi

    if [ "$MSG_COUNT" -gt 0 ]; then
        HAS_NEW_MESSAGES=1
    fi
fi

# --- Decision ---
if [ "$HAS_NEW_MESSAGES" -eq 1 ]; then
    echo "[$START_ISO] New messages found — dispatching $JOB_NAME" | tee "$LOG_FILE"
    exec "$SCRIPT_DIR/dispatch-job.sh" "$JOB_NAME"
else
    echo "[$START_ISO] No new messages since $LAST_TS — skipping dispatch for $JOB_NAME" | tee "$LOG_FILE"
    exit 0
fi
