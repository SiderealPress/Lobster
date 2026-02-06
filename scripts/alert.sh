#!/bin/bash
#===============================================================================
# Hyperion Alert - Send alerts via available channels
#
# Usage: ~/hyperion/scripts/alert.sh "Alert message"
#
# Sends alerts to:
# 1. Telegram (if configured) - via the existing bot
# 2. Local log file
#===============================================================================

ALERT_LOG="$HOME/hyperion-workspace/logs/alerts.log"
OUTBOX_DIR="$HOME/messages/outbox"
ADMIN_CHAT_ID="${HYPERION_ADMIN_CHAT_ID:-}"

# Ensure directories exist
mkdir -p "$(dirname "$ALERT_LOG")"
mkdir -p "$OUTBOX_DIR"

message="$1"
timestamp=$(date -Iseconds)

# Always log to file
echo "[$timestamp] ALERT: $message" >> "$ALERT_LOG"

# Try to send via Telegram if admin chat ID is configured
if [[ -n "$ADMIN_CHAT_ID" ]]; then
    alert_file="$OUTBOX_DIR/alert_$(date +%s%N).json"
    cat > "$alert_file" << EOF
{
    "chat_id": $ADMIN_CHAT_ID,
    "text": "🚨 **Hyperion Alert**\n\n$message\n\n_$(date)_",
    "source": "telegram"
}
EOF
    echo "[$timestamp] Alert sent to Telegram chat $ADMIN_CHAT_ID" >> "$ALERT_LOG"
fi

echo "Alert logged: $message"
