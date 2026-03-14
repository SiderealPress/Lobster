#!/bin/bash
# Health check for lobster-whatsapp-bridge service.
# Called by periodic-self-check.sh when the WhatsApp bridge is installed.
# Writes alert messages to the Lobster inbox if the service is down.

set -euo pipefail

BRIDGE_STATUS=$(systemctl is-active lobster-whatsapp-bridge 2>/dev/null || echo "not-installed")
HEARTBEAT_FILE="${WA_HEARTBEAT_FILE:-/home/admin/lobster-workspace/logs/whatsapp-heartbeat}"
INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
STALE_THRESHOLD_SECONDS=600  # 10 minutes

# Ensure inbox directory exists
mkdir -p "$INBOX_DIR"

# Alert if service is not active
if [ "$BRIDGE_STATUS" != "active" ]; then
    echo "WARN: lobster-whatsapp-bridge is $BRIDGE_STATUS"

    MSG_ID="wa_health_$(date +%s)"
    MSG_FILE="$INBOX_DIR/${MSG_ID}.json"

    cat > "$MSG_FILE" << MSGJSON
{
  "id": "${MSG_ID}",
  "source": "system",
  "type": "system",
  "subtype": "bridge_down",
  "chat_id": "system",
  "user_id": "system",
  "user_name": "Health Check",
  "text": "[Health Check] lobster-whatsapp-bridge is ${BRIDGE_STATUS} — run: sudo systemctl start lobster-whatsapp-bridge",
  "is_group": false,
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
MSGJSON

    echo "Alert written to $MSG_FILE"
fi

# Check heartbeat file freshness
if [ "$BRIDGE_STATUS" = "active" ] && [ -f "$HEARTBEAT_FILE" ]; then
    LAST_BEAT=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || stat -f %m "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE=$((NOW - LAST_BEAT))

    if [ "$AGE" -gt "$STALE_THRESHOLD_SECONDS" ]; then
        echo "WARN: WhatsApp bridge heartbeat is ${AGE}s old (threshold: ${STALE_THRESHOLD_SECONDS}s)"

        MSG_ID="wa_stale_$(date +%s)"
        MSG_FILE="$INBOX_DIR/${MSG_ID}.json"

        cat > "$MSG_FILE" << MSGJSON
{
  "id": "${MSG_ID}",
  "source": "system",
  "type": "system",
  "subtype": "bridge_stale",
  "chat_id": "system",
  "user_id": "system",
  "user_name": "Health Check",
  "text": "[Health Check] WhatsApp bridge is running but no messages received for ${AGE}s — may be stuck",
  "is_group": false,
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
MSGJSON
    fi
fi

exit 0
