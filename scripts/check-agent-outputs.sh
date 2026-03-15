#!/bin/bash
#===============================================================================
# check-agent-outputs.sh — Agent Output Polling Safety Net
#
# Cron: */5 * * * * /home/admin/lobster/scripts/check-agent-outputs.sh # LOBSTER-AGENT-RELAY
#
# Purpose:
#   Safety net for the primary task-notification mechanism. When Lobster misses
#   a <task-notification> block (e.g. context reset, busy loop), this script
#   detects stale output files for agents still listed in pending-agents.json
#   and injects an inbox message so Lobster will notice and relay results.
#
# Logic:
#   1. Read pending-agents.json
#   2. For each agent ID, check if an output file exists at the known tasks dir
#   3. If output file is present AND hasn't been modified in > 2 minutes AND
#      the agent is still in pending-agents.json → inject inbox notification
#   4. Each agent ID is only notified once (tracked in .state/agent-relay-notified)
#
# This is a fallback, not the primary mechanism. The primary relay is the
# <task-notification> block that arrives on wait_for_messages() return.
#===============================================================================

set -euo pipefail

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
INBOX_DIR="$MESSAGES_DIR/inbox"
CONFIG_DIR="$MESSAGES_DIR/config"
PENDING_AGENTS_FILE="$CONFIG_DIR/pending-agents.json"
STATE_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}/.state"
NOTIFIED_FILE="$STATE_DIR/agent-relay-notified"
TASKS_DIR="/tmp/claude-1000/-home-admin-lobster-workspace/tasks"

# Staleness threshold: output file unchanged for this long = agent likely done
COMPLETION_THRESHOLD_SECONDS=120

mkdir -p "$STATE_DIR" "$INBOX_DIR"
touch "$NOTIFIED_FILE" 2>/dev/null || true

# Bail early if pending-agents.json doesn't exist or has no agents
if [ ! -f "$PENDING_AGENTS_FILE" ]; then
    exit 0
fi

# Read agent IDs from pending-agents.json using python3
PENDING_IDS=$(python3 -c "
import json, sys
try:
    with open('$PENDING_AGENTS_FILE') as f:
        data = json.load(f)
    agents = data.get('agents', [])
    for a in agents:
        aid = a.get('id', '').strip()
        if aid:
            print(aid)
except Exception as e:
    sys.exit(0)
" 2>/dev/null) || true

if [ -z "$PENDING_IDS" ]; then
    exit 0
fi

# Bail early if tasks directory doesn't exist
if [ ! -d "$TASKS_DIR" ]; then
    exit 0
fi

NOW=$(date +%s)

while IFS= read -r agent_id; do
    [ -z "$agent_id" ] && continue

    # Skip if already notified
    if grep -q "^${agent_id}$" "$NOTIFIED_FILE" 2>/dev/null; then
        continue
    fi

    # Look for the output file
    OUTPUT_FILE="$TASKS_DIR/${agent_id}.output"
    if [ ! -f "$OUTPUT_FILE" ]; then
        continue
    fi

    # Check if the file hasn't been modified recently (completion heuristic)
    FILE_MTIME=$(stat -c %Y "$OUTPUT_FILE" 2>/dev/null || echo "$NOW")
    AGE=$(( NOW - FILE_MTIME ))

    if [ "$AGE" -lt "$COMPLETION_THRESHOLD_SECONDS" ]; then
        continue
    fi

    # Check the file has content (not a zero-byte placeholder)
    FILE_SIZE=$(stat -c %s "$OUTPUT_FILE" 2>/dev/null || echo "0")
    if [ "$FILE_SIZE" -eq 0 ]; then
        continue
    fi

    # All checks passed: inject inbox notification
    TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%S.%6N)
    EPOCH_MS=$(date +%s%3N)
    MSG_ID="${EPOCH_MS}_agent_relay"

    # Safely escape agent_id for JSON (alphanumeric + hyphens only expected)
    SAFE_ID=$(echo "$agent_id" | tr -cd 'a-zA-Z0-9_-')

    cat > "${INBOX_DIR}/${MSG_ID}.json" << EOF
{
  "id": "${MSG_ID}",
  "source": "system",
  "type": "system",
  "chat_id": 0,
  "user_id": 0,
  "username": "lobster-system",
  "user_name": "Agent Relay",
  "text": "Agent relay check: ${SAFE_ID} output file appears complete. Check task-notifications and relay results to Drew.",
  "timestamp": "${TIMESTAMP}"
}
EOF

    # Mark this agent as notified to prevent re-injection
    echo "$agent_id" >> "$NOTIFIED_FILE"

done <<< "$PENDING_IDS"
