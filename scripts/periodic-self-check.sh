#!/bin/bash
#===============================================================================
# Periodic Self-Check (Cron-based)
#
# Runs every 3 minutes via cron. Injects a self-check message into the Lobster
# inbox ONLY if a Claude Code session is actively running. This is the
# bulletproof fallback that doesn't depend on MCP hooks or tool-call triggers.
#
# Install: Add to crontab with:
#   */3 * * * * $HOME/lobster/scripts/periodic-self-check.sh
#
# Guards:
#   1. Only fires if a Claude Code process is running
#   2. Only fires if there isn't already a self-check in the inbox (no spam)
#   3. Rate-limited: won't inject if last self-check was < 2 minutes ago
#   4. Max inbox depth: won't inject if inbox already has 20+ messages (backpressure)
#===============================================================================

set -e

INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
STATE_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}/.state"
LAST_CHECK_FILE="$STATE_DIR/last-self-check"
LOBSTER_STATE_FILE="$MESSAGES_DIR/config/lobster-state.json"
MAX_INBOX_DEPTH=20

mkdir -p "$INBOX_DIR" "$STATE_DIR"

# Guard 0: Lifecycle check — don't inject during hibernate/backoff/starting
if [ -f "$LOBSTER_STATE_FILE" ]; then
    LOBSTER_MODE=$(python3 -c "
import json
try:
    d = json.load(open('$LOBSTER_STATE_FILE'))
    print(d.get('mode', 'unknown'))
except: print('unknown')
" 2>/dev/null || echo "unknown")
    case "$LOBSTER_MODE" in
        hibernate|backoff|starting|restarting|waking|stopped)
            exit 0
            ;;
    esac
fi

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

# Guard 4: Backpressure — don't add to an already-deep inbox
INBOX_COUNT=$(find "$INBOX_DIR" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)
if [ "$INBOX_COUNT" -ge "$MAX_INBOX_DEPTH" ]; then
    exit 0
fi

# Guard 5: Subagent check — only self-check when subagents are running
# If claude count is <= 1, only the main session exists (no subagents to check on)
CLAUDE_COUNT=$(pgrep -c -f "claude" 2>/dev/null || echo "0")

# Source agent status scanner for both status and completion detection
AGENT_STATUS_SCRIPT="${LOBSTER_INSTALL_DIR:-$HOME/lobster}/scripts/agent-status.sh"
source "$AGENT_STATUS_SCRIPT"

# Check for completed tasks first (works even if subagents already exited)
COMPLETED_TASKS=$(scan_completed_tasks)

if [ -n "$COMPLETED_TASKS" ]; then
    # Completed task found — inject structured completion message.
    # This replaces the generic "status?" prompt with actionable info,
    # so the dispatcher can relay results directly without LLM reasoning
    # about what to check.
    SELF_CHECK_TEXT="[Task Completed] ${COMPLETED_TASKS}"
else
    # Check pending-agents.json tracker — subagents may have already exited
    # but still need relay to Drew (processes gone, record still in file).
    PENDING_AGENTS_FILE="${MESSAGES_DIR}/config/pending-agents.json"
    PENDING_COUNT=$(python3 -c "
import json, sys
try:
    with open('$PENDING_AGENTS_FILE') as f:
        data = json.load(f)
    print(len(data.get('agents', [])))
except Exception:
    print(0)
" 2>/dev/null || echo "0")

    # No completed tasks — only inject status check if subagents are still
    # running OR there are pending agents in the tracker.
    if [ "$CLAUDE_COUNT" -le 1 ] && [ "$PENDING_COUNT" -eq 0 ] 2>/dev/null; then
        exit 0
    fi

    AGENT_SUMMARY=$(scan_agent_status)

    SELF_CHECK_TEXT="status? (Self-check)"
    if [ -n "$AGENT_SUMMARY" ]; then
        SELF_CHECK_TEXT="status? (Self-check) | ${AGENT_SUMMARY}"
    fi
    if [ "$PENDING_COUNT" -gt 0 ] 2>/dev/null; then
        SELF_CHECK_TEXT="${SELF_CHECK_TEXT} [${PENDING_COUNT} agents pending]"
    fi
fi

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
  "text": "${SELF_CHECK_TEXT}",
  "timestamp": "${TIMESTAMP}"
}
EOF

# Record timestamp
date +%s > "$LAST_CHECK_FILE"
