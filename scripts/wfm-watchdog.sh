#!/bin/bash
#===============================================================================
# wfm-watchdog.sh — External watchdog for wait_for_messages() freezes
#
# Runs every 10 minutes via cron.  Checks whether wait_for_messages has been
# running for longer than WFM_WATCHDOG_THRESHOLD_SECONDS (default: 2100s = 35
# minutes, which is 5 minutes past the standard WFM timeout of 1800s).
#
# If a freeze is detected:
#   1. Writes a synthetic wfm_watchdog message to the inbox so WFM's file
#      watcher fires and WFM returns.
#   2. Sends a Telegram alert so the operator knows it happened.
#
# The MCP server writes ~/messages/config/wfm-active.json when WFM starts and
# deletes it when WFM returns normally.  If the file is present and its
# started_at timestamp is older than the threshold, WFM is considered frozen.
#===============================================================================

set -euo pipefail

LOBSTER_DIR="${LOBSTER_DIR:-$HOME/lobster}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
INBOX_DIR="$MESSAGES_DIR/inbox"
CONFIG_DIR="$MESSAGES_DIR/config"
LOGS_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs"
CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"

WFM_ACTIVE_FILE="$CONFIG_DIR/wfm-active.json"

# 35 minutes: 5 min past the standard WFM timeout of 1800s.
# If WFM hasn't returned within this window, it is frozen.
WFM_WATCHDOG_THRESHOLD_SECONDS=2100

# Dedup: don't fire more than once per hour for the same freeze event.
# We use a lockfile stamped with the hour to bound alert volume.
DEDUP_DIR="$CONFIG_DIR"
DEDUP_LOCKFILE="$DEDUP_DIR/wfm-watchdog-fired-$(date -u '+%Y%m%d%H').lock"

LOG_FILE="$LOGS_DIR/wfm-watchdog.log"

log() {
    local level="$1"; shift
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$level] $*" >> "$LOG_FILE" 2>/dev/null || true
}

send_telegram_alert() {
    local message="$1"
    local bot_token=""
    local chat_id=""

    if [[ -f "$CONFIG_ENV" ]]; then
        bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$CONFIG_ENV" | cut -d'=' -f2- || true)
        chat_id=$(grep '^TELEGRAM_ALLOWED_USERS=' "$CONFIG_ENV" | cut -d'=' -f2- | cut -d',' -f1 || true)
    fi

    if [[ -z "$bot_token" || -z "$chat_id" ]]; then
        log "WARN" "Cannot send Telegram alert: missing bot token or chat ID"
        return 1
    fi

    local full_message
    full_message="$(printf '🔁 *Lobster WFM Watchdog*\n\n%s\n\n_%s_' \
        "$message" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')")"

    curl -s -X POST \
        "https://api.telegram.org/bot${bot_token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${full_message}" \
        --data-urlencode "parse_mode=Markdown" \
        --max-time 10 \
        > /dev/null 2>&1 || true
}

write_watchdog_inbox_message() {
    local now_iso
    now_iso=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    local msg_id="wfm-watchdog-$(date -u '+%s')"
    local inbox_file="$INBOX_DIR/${msg_id}.json"
    local tmp_file="$INBOX_DIR/.${msg_id}.tmp"

    cat > "$tmp_file" <<EOF
{
  "id": "$msg_id",
  "source": "system",
  "type": "wfm_watchdog",
  "chat_id": 0,
  "text": "WFM watchdog triggered: wait_for_messages was frozen for more than ${WFM_WATCHDOG_THRESHOLD_SECONDS}s. Call wait_for_messages again to resume the main loop.",
  "timestamp": "$now_iso"
}
EOF
    mv "$tmp_file" "$inbox_file"
    log "INFO" "Wrote watchdog inbox message: $inbox_file"
}

main() {
    mkdir -p "$LOGS_DIR" "$DEDUP_DIR" "$INBOX_DIR" 2>/dev/null || true

    # No active file means WFM is not running or returned normally — nothing to do.
    if [[ ! -f "$WFM_ACTIVE_FILE" ]]; then
        log "DEBUG" "wfm-active.json absent — WFM not running or returned normally"
        exit 0
    fi

    # Parse started_at from the active file.
    local started_at
    started_at=$(python3 -c "
import json, sys
try:
    d = json.load(open('$WFM_ACTIVE_FILE'))
    print(d.get('started_at', ''))
except Exception as e:
    sys.exit(1)
" 2>/dev/null || true)

    if [[ -z "$started_at" ]]; then
        log "WARN" "wfm-active.json exists but could not parse started_at — skipping"
        exit 0
    fi

    # Compute age in seconds.
    local started_epoch
    started_epoch=$(python3 -c "
from datetime import datetime, timezone
dt = datetime.fromisoformat('$started_at')
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
print(int(dt.timestamp()))
" 2>/dev/null || true)

    if [[ -z "$started_epoch" ]]; then
        log "WARN" "Could not compute epoch from started_at=$started_at — skipping"
        exit 0
    fi

    local now_epoch
    now_epoch=$(date -u '+%s')
    local age_seconds=$(( now_epoch - started_epoch ))

    log "DEBUG" "WFM age: ${age_seconds}s (threshold: ${WFM_WATCHDOG_THRESHOLD_SECONDS}s)"

    if [[ "$age_seconds" -lt "$WFM_WATCHDOG_THRESHOLD_SECONDS" ]]; then
        # WFM is running but within normal bounds.
        exit 0
    fi

    # WFM is frozen.  Check dedup lockfile to avoid alert flooding.
    if [[ -f "$DEDUP_LOCKFILE" ]]; then
        log "INFO" "WFM frozen for ${age_seconds}s but dedup lockfile present — skipping alert (already fired this hour)"
        # Still write the inbox message to unfreeze WFM even if we skip the alert.
        write_watchdog_inbox_message
        exit 0
    fi

    log "WARN" "WFM frozen for ${age_seconds}s — triggering watchdog"

    # Mark dedup to suppress repeat alerts this hour.
    touch "$DEDUP_LOCKFILE"

    # Inject wakeup message into inbox so the file watcher fires.
    write_watchdog_inbox_message

    # Send Telegram alert (best-effort — failure here must not abort the watchdog).
    send_telegram_alert "wait_for_messages has been running for ${age_seconds}s (threshold: ${WFM_WATCHDOG_THRESHOLD_SECONDS}s). A wakeup message was injected into the inbox. If the dispatcher is alive, it will resume automatically." || true

    log "INFO" "Watchdog fired: injected inbox message and sent Telegram alert"
}

main "$@"
