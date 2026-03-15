#!/bin/bash
#===============================================================================
# Lobster Health Check
#
# Monitors inbox for stale messages and restarts Claude if stuck.
# Run via cron every 5 minutes: */5 * * * * ~/lobster/scripts/health-check.sh
#===============================================================================

INBOX_DIR="$HOME/messages/inbox"
MAX_AGE_MINUTES=10
LOG_FILE="$HOME/lobster-workspace/logs/health-check.log"

log() {
    echo "[$(date -Iseconds)] $1" >> "$LOG_FILE"
}

# Check for stale inbox messages
stale_count=0
now=$(date +%s)

for f in "$INBOX_DIR"/*.json 2>/dev/null; do
    [ -f "$f" ] || continue
    file_age=$(stat -c %Y "$f")
    age_minutes=$(( (now - file_age) / 60 ))

    if [ "$age_minutes" -ge "$MAX_AGE_MINUTES" ]; then
        stale_count=$((stale_count + 1))
        log "STALE: $f is ${age_minutes}m old"
    fi
done

if [ "$stale_count" -gt 0 ]; then
    log "WARNING: $stale_count stale message(s) detected. Restarting lobster-claude..."

    # Kill the tmux session and let systemd restart it
    tmux -L lobster kill-session -t lobster 2>/dev/null
    sleep 2
    sudo systemctl restart lobster-claude

    log "Restarted lobster-claude service"
else
    # Touch a heartbeat file to show health check is running
    touch "$HOME/lobster-workspace/logs/health-check.heartbeat"
fi

# WhatsApp bridge health check
WA_STATUS=$(systemctl is-active lobster-whatsapp-bridge 2>/dev/null || echo "unknown")
if [ "$WA_STATUS" != "active" ] && [ "$WA_STATUS" != "unknown" ]; then
    # Inject alert to Lobster inbox
    ALERT_FILE=~/messages/inbox/wa_health_$(date +%s).json
    cat > "$ALERT_FILE" <<EOF
{
    "id": "wa_health_$(date +%s)",
    "source": "system",
    "chat_id": "system",
    "text": "[Health] lobster-whatsapp-bridge is $WA_STATUS",
    "timestamp": "$(date -Iseconds)"
}
EOF
    log "ALERT: lobster-whatsapp-bridge is $WA_STATUS — injected inbox alert"
fi

# Check heartbeat (last message received)
HEARTBEAT=~/lobster-workspace/logs/whatsapp-heartbeat
if [ -f "$HEARTBEAT" ]; then
    LAST=$(cat "$HEARTBEAT")
    NOW=$(date +%s%3N)
    AGE=$(( ($NOW - $LAST) / 1000 ))
    if [ $AGE -gt 3600 ]; then
        log "WARN: WhatsApp bridge - no messages for ${AGE}s"
    fi
fi
