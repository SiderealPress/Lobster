#!/usr/bin/env bash
# restart-mcp.sh — Safe wrapper for restarting the lobster-mcp-local service.
#
# USE THIS SCRIPT instead of calling `sudo systemctl restart lobster-mcp-local`
# directly.  Direct restarts invalidate the active MCP session immediately,
# leaving the dispatcher blocked in wait_for_messages with a "Session not found"
# error and no recovery guidance.
#
# This script:
#   1. Writes an mcp-restart warning message to ~/messages/inbox/
#   2. Waits 2 seconds for the dispatcher to process it
#   3. Runs `sudo systemctl restart lobster-mcp-local`
#
# The inbox message tells the dispatcher the restart is intentional and that
# it should re-orient after reconnecting.  Combined with Fix 1
# (session-lost-reminder written on server startup), the dispatcher has two
# chances to see recovery guidance.
#
# Usage:
#   ~/lobster/scripts/restart-mcp.sh
#   ~/lobster/scripts/restart-mcp.sh --no-wait   (skip 2s delay, for scripted use)

set -euo pipefail

INBOX_DIR="${LOBSTER_MESSAGES:-${HOME}/messages}/inbox"
REASON="${1:-manual restart}"
NO_WAIT=false
if [[ "${1:-}" == "--no-wait" ]]; then
    NO_WAIT=true
fi

# Write the warning message to the inbox
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MSG_ID="mcp-restart-$(date -u +%s)"
MSG_FILE="${INBOX_DIR}/${MSG_ID}.json"

mkdir -p "${INBOX_DIR}"

cat > "${MSG_FILE}.tmp" <<EOF
{
  "id": "${MSG_ID}",
  "source": "system",
  "type": "compact-reminder",
  "chat_id": 0,
  "text": "MCP RESTART INCOMING — The lobster-mcp-local service is about to restart. Your MCP session will be invalidated. Re-orient after reconnecting: read sys.dispatcher.bootup.md and resume the main loop.",
  "timestamp": "${TIMESTAMP}"
}
EOF
mv "${MSG_FILE}.tmp" "${MSG_FILE}"

echo "[restart-mcp] Wrote restart warning to inbox: ${MSG_ID}"

if [[ "${NO_WAIT}" == "false" ]]; then
    echo "[restart-mcp] Waiting 2s for dispatcher to see the message..."
    sleep 2
fi

echo "[restart-mcp] Restarting lobster-mcp-local..."
sudo systemctl restart lobster-mcp-local
echo "[restart-mcp] Service restarted."
