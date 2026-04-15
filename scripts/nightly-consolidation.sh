#!/bin/bash
# Nightly Consolidation - Cron wrapper script
#
# Injects a consolidation task message into the inbox for the running
# Claude session to process. Claude handles the actual synthesis using
# its MCP memory tools (memory_recent, mark_consolidated, etc.).
#
# No direct API calls are made here. Everything goes through Claude Code.
#
# Crontab entry:
#   0 3 * * * $HOME/lobster/scripts/nightly-consolidation.sh >> $HOME/lobster-workspace/logs/nightly-consolidation.log 2>&1
#
# Dedup guard: if a consolidation message is already pending in the inbox,
# this script exits without writing a duplicate.

set -euo pipefail

# Developer mode: suppress all system notifications so the developer isn't
# bothered while testing. Real user messages are never affected by this flag.
_LOBSTER_CONFIG="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"
if [ -f "$_LOBSTER_CONFIG" ]; then
    _DEV_MODE=$(grep -m1 '^LOBSTER_DEV_MODE=' "$_LOBSTER_CONFIG" 2>/dev/null | cut -d= -f2 || true)
    if [ "$_DEV_MODE" = "true" ] || [ "$_DEV_MODE" = "1" ]; then
        exit 0
    fi
fi
unset _LOBSTER_CONFIG _DEV_MODE

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOBSTER_DIR="$(dirname "$SCRIPT_DIR")"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
INBOX="$MESSAGES_DIR/inbox"
TIMESTAMP=$(date +%s%3N)
LOG_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs"
LOG_FILE="$LOG_DIR/nightly-consolidation.log"

# Ensure directories exist
mkdir -p "$INBOX" "$LOG_DIR"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"
}

log "nightly-consolidation.sh started"

# Dedup guard: skip if a consolidation message is already pending
if ls "$INBOX"/*_consolidation.json 2>/dev/null | grep -q .; then
    log "Consolidation message already pending in inbox. Skipping."
    exit 0
fi

# Inject a consolidation message for the running Claude session
cat > "$INBOX/${TIMESTAMP}_consolidation.json" << EOF
{
  "id": "${TIMESTAMP}_consolidation",
  "source": "internal",
  "chat_id": 0,
  "type": "consolidation",
  "text": "NIGHTLY CONSOLIDATION: Review today's events using memory_recent(hours=24) and update canonical memory files. Steps:\n1. Call memory_recent(hours=24) to get all events from the past day\n2. Synthesize key themes, decisions, and action items\n3. Update memory/canonical/daily-digest.md with the synthesis\n4. Update memory/canonical/priorities.md if priorities changed\n5. Update relevant project files in memory/canonical/projects/\n6. Update people files if new relationship info emerged\n7. Mark all reviewed events as consolidated using mark_consolidated\n8. Update memory/canonical/handoff.md with current state",
  "timestamp": "$(date -Iseconds)"
}
EOF

log "Consolidation message injected at $(date -Iseconds)"
