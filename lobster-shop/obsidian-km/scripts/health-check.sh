#!/bin/bash
#===============================================================================
# CouchDB Health Check for Obsidian KM Skill
#
# Verifies:
#   1. CouchDB systemd service is running
#   2. Port 5984 is responding
#   3. HTTP auth is working
#
# Exit codes:
#   0 - CouchDB is healthy
#   1 - Service not running
#   2 - HTTP check failed
#   3 - Auth failed
#
# Usage: ~/lobster/lobster-shop/obsidian-km/scripts/health-check.sh
#===============================================================================

set -o pipefail

#===============================================================================
# Configuration
#===============================================================================
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
OBSIDIAN_ENV="$CONFIG_DIR/obsidian.env"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOG_FILE="$WORKSPACE_DIR/logs/couchdb-health.log"
ALERT_LOG="$WORKSPACE_DIR/logs/alerts.log"
COUCHDB_HOST="${COUCHDB_HOST:-127.0.0.1}"
COUCHDB_PORT="${COUCHDB_PORT:-5984}"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$ALERT_LOG")"

#===============================================================================
# Logging
#===============================================================================
log() {
    echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"
}
log_info()  { log "INFO"  "$1"; }
log_warn()  { log "WARN"  "$1"; }
log_error() { log "ERROR" "$1"; }

#===============================================================================
# Alert function - sends to Telegram via Lobster's outbox
#===============================================================================
send_alert() {
    local message="$1"
    local timestamp=$(date -Iseconds)
    local outbox_dir="${LOBSTER_MESSAGES:-$HOME/messages}/outbox"

    # Log alert
    echo "[$timestamp] ALERT: $message" >> "$ALERT_LOG"
    log_error "ALERT: $message"

    # Load admin chat ID from config
    local admin_chat_id=""
    if [[ -f "$CONFIG_DIR/config.env" ]]; then
        admin_chat_id=$(grep '^TELEGRAM_ALLOWED_USERS=' "$CONFIG_DIR/config.env" 2>/dev/null | cut -d'=' -f2- | cut -d',' -f1)
    fi

    # Send via outbox if we have a chat ID
    if [[ -n "$admin_chat_id" ]]; then
        mkdir -p "$outbox_dir"
        local alert_file="$outbox_dir/couchdb_alert_$(date +%s%N).json"
        cat > "$alert_file" << EOF
{
    "chat_id": $admin_chat_id,
    "text": "CouchDB Health Alert\n\n$message\n\n$(date)",
    "source": "telegram"
}
EOF
        log_info "Alert sent to Telegram chat $admin_chat_id"
    fi
}

#===============================================================================
# Load credentials
#===============================================================================
load_credentials() {
    if [[ ! -f "$OBSIDIAN_ENV" ]]; then
        log_error "Config file not found: $OBSIDIAN_ENV"
        send_alert "CouchDB health check failed: missing config file $OBSIDIAN_ENV"
        echo "Config file not found: $OBSIDIAN_ENV"
        exit 1
    fi

    # shellcheck source=/dev/null
    source "$OBSIDIAN_ENV"

    if [[ -z "${COUCHDB_USER:-}" || -z "${COUCHDB_PASSWORD:-}" ]]; then
        log_error "CouchDB credentials not configured in $OBSIDIAN_ENV"
        send_alert "CouchDB health check failed: credentials not configured"
        echo "CouchDB credentials not configured"
        exit 1
    fi
}

#===============================================================================
# Check 1: Is the CouchDB service running?
#===============================================================================
check_service() {
    log_info "Checking CouchDB service status..."

    if ! systemctl --user is-active --quiet couchdb 2>/dev/null; then
        log_error "CouchDB systemd service is not running"
        send_alert "CouchDB service is not running. Check: systemctl --user status couchdb"
        echo "CouchDB service is not running"
        return 1
    fi

    log_info "CouchDB service is active"
    return 0
}

#===============================================================================
# Check 2: Is port 5984 responding?
#===============================================================================
check_port() {
    log_info "Checking CouchDB HTTP endpoint..."

    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 10 \
        "http://${COUCHDB_HOST}:${COUCHDB_PORT}/" 2>/dev/null)

    if [[ "$http_status" == "000" ]]; then
        log_error "CouchDB not responding on port ${COUCHDB_PORT}"
        send_alert "CouchDB HTTP endpoint not responding on port ${COUCHDB_PORT}"
        echo "CouchDB HTTP endpoint not responding"
        return 2
    fi

    log_info "CouchDB HTTP endpoint responding (status: $http_status)"
    return 0
}

#===============================================================================
# Check 3: Is authentication working?
#===============================================================================
check_auth() {
    log_info "Checking CouchDB authentication..."

    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 10 \
        "http://${COUCHDB_USER}:${COUCHDB_PASSWORD}@${COUCHDB_HOST}:${COUCHDB_PORT}/" 2>/dev/null)

    if [[ "$http_status" != "200" ]]; then
        log_error "CouchDB auth check failed: status $http_status"
        send_alert "CouchDB authentication failed (status $http_status). Check credentials in $OBSIDIAN_ENV"
        echo "CouchDB HTTP check failed: status $http_status"
        return 3
    fi

    log_info "CouchDB authentication successful"
    return 0
}

#===============================================================================
# Main
#===============================================================================
main() {
    log_info "Starting CouchDB health check"

    # Load credentials first
    load_credentials

    # Run checks in order
    if ! check_service; then
        exit 1
    fi

    if ! check_port; then
        exit 2
    fi

    if ! check_auth; then
        exit 3
    fi

    log_info "CouchDB healthy"
    echo "CouchDB healthy"
    exit 0
}

main "$@"
