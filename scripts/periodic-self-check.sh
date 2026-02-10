#!/bin/bash
#===============================================================================
# Periodic Self-Check (Cron-based)
#
# Runs every 3 minutes via cron. Injects a self-check message into the Lobster
# inbox ONLY if a Claude Code session is actively running. This is the
# bulletproof fallback that doesn't depend on MCP hooks or tool-call triggers.
#
# Install: Add to crontab with:
#   */3 * * * * /home/admin/lobster/scripts/periodic-self-check.sh
#
# Guards:
#   1. Only fires if a Claude Code process is running
#   2. Only fires if there isn't already a self-check in the inbox (no spam)
#   3. Rate-limited: won't inject if last self-check was < 2 minutes ago
#===============================================================================

set -e

INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
STATE_DIR="$HOME/lobster/.state"
LAST_CHECK_FILE="$STATE_DIR/last-self-check"

mkdir -p "$INBOX_DIR" "$STATE_DIR"

# Guard 1: Is Claude Code running?
if ! pgrep -f "claude" > /dev/null 2>&1; then
    exit 0
fi

# Guard 2: Is there already a self-check message in the inbox?
if compgen -G "$INBOX_DIR"/*_self.json > /dev/null 2>&1; then
    exit 0
fi

# Guard 3: Rate limit — skip if last check was less than 2 minutes ago
if [ -f "$LAST_CHECK_FILE" ]; then
    LAST_CHECK=$(cat "$LAST_CHECK_FILE")
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST_CHECK))
    if [ "$ELAPSED" -lt 120 ]; then
        exit 0
    fi
fi

# All guards passed — inject self-check
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%S.%6N)
EPOCH_MS=$(date +%s%3N)
MSG_ID="${EPOCH_MS}_self"

cat > "${INBOX_DIR}/${MSG_ID}.json" << EOF
{
  "id": "${MSG_ID}",
  "source": "system",
  "chat_id": 0,
  "user_id": 0,
  "username": "lobster-system",
  "user_name": "Self-Check",
  "text": "status? (Self-check)",
  "timestamp": "${TIMESTAMP}"
}
EOF

# Record timestamp
date +%s > "$LAST_CHECK_FILE"
