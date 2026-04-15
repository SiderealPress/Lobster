#!/bin/bash
#===============================================================================
# Lobster Token Refresh - OAuth Token Health Check
#
# Verifies that the Claude OAuth token (set via CLAUDE_CODE_OAUTH_TOKEN in
# config.env) is still valid by running `claude auth status`. Sends a Telegram
# alert if auth has failed.
#
# Auth is managed via CLAUDE_CODE_OAUTH_TOKEN env var — there is no credentials
# file to refresh. Token renewal requires updating CLAUDE_CODE_OAUTH_TOKEN in
# ~/lobster-config/config.env and restarting the service.
#
# Install via cron (every 2 hours):
#   0 */2 * * * $HOME/lobster/scripts/token-refresh.sh
#
# How it works:
#   1. Runs `claude auth status` (outputs JSON by default — do NOT pass
#      --output-format json, that flag does not exist and causes exit code 1)
#   2. If loggedIn=false after ALERT_AFTER_FAILURES consecutive checks, alert
#   3. If loggedIn=true, reset failure counter and exit 0
#===============================================================================

set -o pipefail

LOG_FILE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/token-refresh.log"
CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"

FAILURE_COUNTER_FILE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/token-refresh-failures"
ALERT_COOLDOWN_FILE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/token-refresh-last-alert"
ALERT_AFTER_FAILURES=2   # alert on the 2nd consecutive failed check
ALERT_COOLDOWN_SECONDS=43200  # 12-hour cooldown between alerts

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date -Iseconds)] $1" >> "$LOG_FILE"
}

should_alert() {
    if [[ -f "$ALERT_COOLDOWN_FILE" ]]; then
        local last_alert
        last_alert=$(cat "$ALERT_COOLDOWN_FILE" 2>/dev/null || echo 0)
        local now
        now=$(date +%s)
        local elapsed=$(( now - last_alert ))
        if [[ $elapsed -lt $ALERT_COOLDOWN_SECONDS ]]; then
            local remaining_cooldown=$(( (ALERT_COOLDOWN_SECONDS - elapsed) / 3600 ))
            log "Alert suppressed (cooldown: ${remaining_cooldown}h remaining)"
            return 1
        fi
    fi
    return 0
}

send_telegram_alert() {
    local message="$1"
    local bot_token="" chat_id=""

    if [[ -f "$CONFIG_ENV" ]]; then
        bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$CONFIG_ENV" | cut -d'=' -f2-)
        chat_id=$(grep '^TELEGRAM_ALLOWED_USERS=' "$CONFIG_ENV" | cut -d'=' -f2- | cut -d',' -f1)
    fi

    [[ -z "$bot_token" || -z "$chat_id" ]] && return 1

    curl -s -X POST \
        "https://api.telegram.org/bot${bot_token}/sendMessage" \
        -d chat_id="$chat_id" \
        -d text="$message" \
        -d parse_mode="Markdown" \
        --max-time 10 \
        > /dev/null 2>&1
}

main() {
    # Auth is via CLAUDE_CODE_OAUTH_TOKEN env var — use `claude auth status`
    # as the single source of truth. No credentials file to read or refresh.
    #
    # NOTE: `claude auth status` outputs JSON by default. Do NOT pass
    # --output-format json — that flag does not exist and causes exit code 1,
    # leaving auth_json empty and making every parse return "unknown".
    # This is consistent with the approach used in health-check-v3.sh.
    local auth_json logged_in auth_method
    auth_json=$(env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT \
        claude auth status 2>/dev/null)

    logged_in=$(echo "$auth_json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('true' if d.get('loggedIn') else 'false')
except:
    print('unknown')
" 2>/dev/null)

    auth_method=$(echo "$auth_json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('authMethod', 'unknown'))
except:
    print('unknown')
" 2>/dev/null)

    if [[ "$logged_in" == "true" ]]; then
        log "Auth OK: loggedIn=true via $auth_method (CLAUDE_CODE_OAUTH_TOKEN)"
        rm -f "$FAILURE_COUNTER_FILE"
        return 0
    fi

    if [[ "$logged_in" == "unknown" ]]; then
        log "Auth check inconclusive: could not parse claude auth status output"
        return 0
    fi

    # loggedIn=false — increment consecutive failure counter
    local failure_count=0
    if [[ -f "$FAILURE_COUNTER_FILE" ]]; then
        failure_count=$(cat "$FAILURE_COUNTER_FILE" 2>/dev/null || echo 0)
    fi
    failure_count=$((failure_count + 1))
    echo "$failure_count" > "$FAILURE_COUNTER_FILE"
    log "Auth FAILED: loggedIn=false (consecutive: $failure_count, alert threshold: $ALERT_AFTER_FAILURES)"

    if [[ $failure_count -ge $ALERT_AFTER_FAILURES ]]; then
        log "ALERT: Auth confirmed failed after $failure_count consecutive checks."
        if should_alert; then
            date +%s > "$ALERT_COOLDOWN_FILE"
            send_telegram_alert "\xf0\x9f\x94\x91 *Auth Failed*

Claude auth check reports loggedIn=false after $failure_count consecutive attempts.

Fix: Update CLAUDE_CODE_OAUTH_TOKEN in ~/lobster-config/config.env and restart:
  systemctl restart lobster-claude"
        fi
        rm -f "$FAILURE_COUNTER_FILE"
    fi
    return 1
}

main "$@"
