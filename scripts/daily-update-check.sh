#!/bin/bash
# Daily check for Lobster updates - inject message if updates available
set -euo pipefail

LOBSTER_DIR="${HOME}/lobster"
INBOX="${HOME}/messages/inbox"

cd "$LOBSTER_DIR"
git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    BEHIND=$(git rev-list --count "$LOCAL..$REMOTE")
    TIMESTAMP=$(date +%s%3N)
    cat > "$INBOX/${TIMESTAMP}_update_available.json" << EOF
{
  "id": "${TIMESTAMP}_update_available",
  "source": "internal",
  "chat_id": 0,
  "type": "update_notification",
  "text": "UPDATE AVAILABLE: Lobster is ${BEHIND} commits behind origin/main. Use check_updates for details.",
  "timestamp": "$(date -Iseconds)"
}
EOF
fi
