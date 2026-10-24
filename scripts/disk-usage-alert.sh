#!/bin/bash
#===============================================================================
# disk-usage-alert.sh — Telegram alert when the root filesystem runs hot.
#
# Checks `df` for MOUNT (default /). If usage exceeds DISK_ALERT_THRESHOLD
# percent (default 85), sends one alert to the admin chat via scripts/alert.sh
# (the same outbox mechanism other jobs use) with the top-5 disk consumers,
# then stays quiet for DISK_ALERT_COOLDOWN_SECS (default 6h) while still high.
# Every run appends one line to the log so the check itself is auditable.
#
# Scheduled hourly by the LOBSTER-MANAGED systemd timer lobster-disk-usage-alert.
#
# Overrides (env):
#   DISK_ALERT_THRESHOLD      percent, default 85
#   DISK_ALERT_MOUNT          default /
#   DISK_ALERT_COOLDOWN_SECS  default 21600
#   DISK_ALERT_STATE          state file (last alert epoch)
#   DISK_ALERT_CHAT_ID        default LOBSTER_ADMIN_CHAT_ID from config.env
#
# Test once with a low threshold and a throwaway state file:
#   DISK_ALERT_THRESHOLD=10 DISK_ALERT_STATE=/tmp/disk-alert.test ./disk-usage-alert.sh
#===============================================================================
set -uo pipefail

LOBSTER_INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
CONFIG_FILE="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"

THRESHOLD="${DISK_ALERT_THRESHOLD:-85}"
MOUNT="${DISK_ALERT_MOUNT:-/}"
COOLDOWN_SECS="${DISK_ALERT_COOLDOWN_SECS:-21600}"
STATE_FILE="${DISK_ALERT_STATE:-$WORKSPACE_DIR/logs/disk-usage-alert.state}"
LOG_FILE="$WORKSPACE_DIR/logs/disk-usage-alert.log"
TOP_N=5

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >> "$LOG_FILE"; }

# --- pure helpers -------------------------------------------------------------

# usage_percent MOUNT -> integer percent used, from df
usage_percent() { df --output=pcent "$1" 2>/dev/null | tail -1 | tr -dc '0-9'; }

# in_cooldown STATE_FILE COOLDOWN NOW -> 0 (true) if an alert was sent < COOLDOWN ago
in_cooldown() {
    local last; last=$(cat "$1" 2>/dev/null || echo 0)
    [[ "$last" =~ ^[0-9]+$ ]] && (( $3 - last < $2 ))
}

# top_consumers N -> N largest level-1 directories under the usual suspects
top_consumers() {
    sudo -n du -xsh /home/lobster/* /home/lobster/.[!.]* /var/lib/docker /var/log /var/cache /tmp /usr /opt /root 2>/dev/null \
        | sort -rh | head -n "$1"
}

build_message() {
    local pct="$1" threshold="$2" top="$3"
    printf 'Root disk at %s%% (threshold %s%%) on %s\n\n%s\n\nTop %s consumers:\n%s\n\nkissinger WAL: %s' \
        "$pct" "$threshold" "$(hostname)" \
        "$(df -h "$MOUNT" | tail -1 | awk '{print $3" used / "$2" total, "$4" free"}')" \
        "$TOP_N" "$top" \
        "$(ls -lh "$HOME/.kissinger/graph.db-wal" 2>/dev/null | awk '{print $5}' || echo n/a)"
}

# --- main ---------------------------------------------------------------------

pct=$(usage_percent "$MOUNT")
if [[ -z "$pct" ]]; then log "ERROR: could not read usage for $MOUNT"; exit 1; fi

if (( pct <= THRESHOLD )); then
    log "ok: $MOUNT at ${pct}% (threshold ${THRESHOLD}%)"
    exit 0
fi

now=$(date +%s)
if in_cooldown "$STATE_FILE" "$COOLDOWN_SECS" "$now"; then
    log "high but in cooldown: $MOUNT at ${pct}% (threshold ${THRESHOLD}%)"
    exit 0
fi

# Chat id: explicit override, else LOBSTER_ADMIN_CHAT_ID from config.env (not inherited by systemd).
chat_id="${DISK_ALERT_CHAT_ID:-}"
if [[ -z "$chat_id" && -f "$CONFIG_FILE" ]]; then
    chat_id=$(grep -m1 '^LOBSTER_ADMIN_CHAT_ID=' "$CONFIG_FILE" | cut -d= -f2 | tr -d '[:space:]"')
fi
if [[ -z "$chat_id" ]]; then log "ERROR: no admin chat id (set LOBSTER_ADMIN_CHAT_ID)"; exit 1; fi

message=$(build_message "$pct" "$THRESHOLD" "$(top_consumers "$TOP_N")")
LOBSTER_ADMIN_CHAT_ID="$chat_id" "$LOBSTER_INSTALL_DIR/scripts/alert.sh" "$message" >/dev/null
echo "$now" > "$STATE_FILE"
log "ALERT sent to chat $chat_id: $MOUNT at ${pct}% (threshold ${THRESHOLD}%)"
