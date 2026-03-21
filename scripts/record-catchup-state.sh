#!/bin/bash
#===============================================================================
# record-catchup-state.sh - Write catchup timestamps to lobster-state.json
#
# Usage:
#   record-catchup-state.sh start    # Write catchup_started_at (call before spawning catchup)
#   record-catchup-state.sh finish   # Write catchup_finished_at (call when result arrives)
#
# Purpose:
#   The health check (health-check-v3.sh) suppresses the WFM freshness check
#   while a catchup subagent is actively running.  Without this, the dispatcher
#   stalls for 10-12 minutes during startup or post-compaction catchup
#   generation — long enough for the 600s WFM threshold to fire and trigger
#   an unnecessary restart.
#
#   The health check reads catchup_started_at and catchup_finished_at from
#   lobster-state.json.  This script writes those fields atomically so the
#   health check can always read a consistent snapshot.
#
# Both calls are safe to make from a Claude subagent context (they are pure
# file operations that complete in <1s and do not require MCP).
#===============================================================================

set -euo pipefail

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
STATE_FILE="${LOBSTER_STATE_FILE_OVERRIDE:-$MESSAGES_DIR/config/lobster-state.json}"

usage() {
    echo "Usage: $0 {start|finish}" >&2
    exit 1
}

[[ $# -eq 1 ]] || usage

action="$1"
case "$action" in
    start|finish) ;;
    *) usage ;;
esac

now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Determine which field to write
if [[ "$action" == "start" ]]; then
    field="catchup_started_at"
else
    field="catchup_finished_at"
fi

# Atomic update: read existing state, add/update field, write back via tmp file.
# Uses python3 (not uv) because this script runs outside the Claude/uv context.
python3 - <<PYEOF
import json, os, sys

state_path = "${STATE_FILE}"
field = "${field}"
now = "${now}"

# Ensure parent directory exists
os.makedirs(os.path.dirname(state_path), exist_ok=True)

# Read existing state (if any)
state = {}
if os.path.exists(state_path):
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        state = {}

state[field] = now

# Atomic write via temp file
tmp_path = state_path + ".tmp"
try:
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, state_path)
except Exception as e:
    sys.exit(f"record-catchup-state: failed to write {state_path}: {e}")
PYEOF

echo "[record-catchup-state] wrote $field=$now to $STATE_FILE"
