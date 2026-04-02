#!/bin/bash
# post-reminder.sh — Drop a scheduled_reminder message into the Lobster inbox.
#
# Usage: post-reminder.sh <reminder_type>
#
# Checks inbox/ and processing/ for an existing unprocessed message of the same
# reminder_type before inserting (dedup). If a duplicate is found, exits 0
# silently. Otherwise writes a JSON message file to ~/messages/inbox/.
#
# This is pure shell — no Python, no Claude. Safe to call from cron.

set -euo pipefail

# Developer mode: suppress all system notifications so the developer isn't
# bothered while testing. Real user messages are never affected by this flag.
_LOBSTER_CONFIG="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"
if [ -f "$_LOBSTER_CONFIG" ]; then
    _DEV_MODE=$(grep -m1 '^LOBSTER_DEV_MODE=' "$_LOBSTER_CONFIG" 2>/dev/null | cut -d= -f2)
    if [ "$_DEV_MODE" = "true" ] || [ "$_DEV_MODE" = "1" ]; then
        exit 0
    fi
fi
unset _LOBSTER_CONFIG _DEV_MODE

REMINDER_TYPE="${1:-}"

if [ -z "$REMINDER_TYPE" ]; then
    echo "Usage: $0 <reminder_type>" >&2
    exit 1
fi

INBOX_DIR="${HOME}/messages/inbox"
PROCESSING_DIR="${HOME}/messages/processing"

mkdir -p "$INBOX_DIR"

# Dedup: skip if an unprocessed message of this reminder_type already exists.
# Check both inbox/ and processing/ — a message in processing/ is still active.
if grep -rl "\"reminder_type\": \"${REMINDER_TYPE}\"" "$INBOX_DIR" "$PROCESSING_DIR" 2>/dev/null | grep -q .; then
    exit 0
fi

TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
MILLIS="$(date +%s%3N)"
FILENAME="${INBOX_DIR}/${MILLIS}_reminder_${REMINDER_TYPE}.json"

cat > "$FILENAME" <<EOF
{
  "type": "scheduled_reminder",
  "reminder_type": "${REMINDER_TYPE}",
  "source": "system",
  "chat_id": 0,
  "text": "Scheduled reminder: ${REMINDER_TYPE}",
  "timestamp": "${TIMESTAMP}"
}
EOF
