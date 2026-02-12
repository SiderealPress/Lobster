#!/bin/bash
# Nightly Consolidation - Inject message into inbox at 3 AM
#
# This script is run by cron. It does NOT call the Claude API directly.
# Instead, it injects a consolidation message into the inbox queue,
# which the running Claude Code session will pick up and process.
#
# Crontab entry:
#   0 3 * * * /home/admin/lobster/scripts/nightly-consolidation.sh

set -euo pipefail

INBOX="$HOME/messages/inbox"
TIMESTAMP=$(date +%s%3N)

# Ensure inbox directory exists
mkdir -p "$INBOX"

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

echo "Consolidation message injected at $(date -Iseconds)"
