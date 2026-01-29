#!/bin/bash
#===============================================================================
# Self-Check Reminder Script
#
# Injects a self-check message into the Hyperion inbox. This allows the main
# Claude session to schedule a one-off reminder to check on background agent
# status or other deferred tasks.
#
# Usage:
#   Direct:  ./self-check-reminder.sh
#   Via at:  echo "$HOME/hyperion/scripts/self-check-reminder.sh" | at now + 3 minutes
#
# The injected message appears as a system message with the text "status? (Self-check)"
# which prompts Hyperion to check on any pending subagent work.
#
# Behavior:
#   - Silent: Only respond to user if there's actual news to report
#   - Default timing: 3 minutes (when scheduling via at)
#   - Max timing: 10 minutes (don't schedule too far out)
#===============================================================================

set -e

# Use environment variable or default
INBOX_DIR="${HYPERION_MESSAGES:-$HOME/messages}/inbox"

# Ensure inbox directory exists
mkdir -p "$INBOX_DIR"

# Generate unique message ID using epoch milliseconds
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%S.%6N)
EPOCH_MS=$(date +%s%3N)
MSG_ID="${EPOCH_MS}_self"

# Create the self-check message
cat > "${INBOX_DIR}/${MSG_ID}.json" << EOF
{
  "id": "${MSG_ID}",
  "source": "system",
  "chat_id": 0,
  "user_id": 0,
  "username": "hyperion-system",
  "user_name": "Self-Check",
  "text": "status? (Self-check)",
  "timestamp": "${TIMESTAMP}"
}
EOF

echo "Self-check reminder injected: ${MSG_ID}"
