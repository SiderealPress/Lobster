#!/usr/bin/env bash
# lobster-plans-check-dispatch.sh
# Layer 1 pre-check for lobster-plans-poller.
#
# Queries the sayhar/lobster-plans GitHub repo for issues with awaiting-decision
# label updated since the last recorded timestamp. If none have changed, logs a
# no-op and exits 0 without writing to the inbox (no LLM spawned).
# If updated issues exist, calls dispatch-job.sh to trigger the full subagent.
#
# Flow:
#   updated issues found  → dispatch-job.sh lobster-plans-poller → inbox → LLM
#   no updated issues     → log no-op → exit 0 (no inbox write)
#
# State file: ~/lobster-workspace/data/lobster-plans-last-seen.txt
#   Contains an ISO 8601 timestamp of the last time an update was dispatched.

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

JOB_NAME="lobster-plans-poller"
REPO_DIR="${REPO_DIR:-$HOME/lobster}"
WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
STATE_FILE="$WORKSPACE/data/lobster-plans-last-seen.txt"
LOG_PREFIX="[lobster-plans-check]"

# --- Load env (for GH_TOKEN if needed) ---
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

# Export GH_TOKEN so gh CLI works from cron
if [ -z "${GH_TOKEN:-}" ] && [ -f "$CONFIG_DIR/config.env" ]; then
    GH_TOKEN=$(grep -E '^GH_TOKEN=' "$CONFIG_DIR/config.env" | cut -d= -f2- | tr -d '"' || true)
    export GH_TOKEN
fi

# --- Read last-seen timestamp ---
if [ -f "$STATE_FILE" ]; then
    LAST_TS=$(cat "$STATE_FILE" | tr -d '[:space:]')
else
    # No state: treat as epoch 0 — process everything on first run
    LAST_TS="1970-01-01T00:00:00Z"
fi

echo "$LOG_PREFIX Checking lobster-plans for issues updated since $LAST_TS"

# --- Query GitHub for awaiting-decision issues updated since last_ts ---
# gh issue list does not support --updated-since directly, so we filter via --json
# and Python. The --state open filter ensures we only look at active issues.
set +e
UPDATED_COUNT=$(gh issue list \
    --repo sayhar/lobster-plans \
    --label "awaiting-decision" \
    --state open \
    --json number,updatedAt \
    --limit 50 2>/dev/null | python3 -c "
import json, sys
from datetime import datetime, timezone

last_ts_str = '$LAST_TS'
try:
    # Parse last timestamp; handle both Z suffix and +00:00
    last_ts = datetime.fromisoformat(last_ts_str.replace('Z', '+00:00'))
except ValueError:
    last_ts = datetime.min.replace(tzinfo=timezone.utc)

try:
    issues = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(2)

count = sum(
    1 for i in issues
    if datetime.fromisoformat(i['updatedAt'].replace('Z', '+00:00')) > last_ts
)
print(count)
" 2>/dev/null)
QUERY_EXIT=$?
set -e

if [ "$QUERY_EXIT" -ne 0 ]; then
    echo "$LOG_PREFIX GitHub query failed or returned invalid JSON — dispatching to let job handle it"
    exec "$REPO_DIR/scheduled-tasks/dispatch-job.sh" "$JOB_NAME"
fi

if [ "${UPDATED_COUNT:-0}" -eq 0 ]; then
    echo "$LOG_PREFIX No awaiting-decision issues updated since $LAST_TS — skipping dispatch"
    exit 0
fi

echo "$LOG_PREFIX $UPDATED_COUNT updated issue(s) found — dispatching to inbox"
exec "$REPO_DIR/scheduled-tasks/dispatch-job.sh" "$JOB_NAME"
