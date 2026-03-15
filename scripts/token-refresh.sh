#!/bin/bash
#===============================================================================
# Lobster Token Refresh - Proactive OAuth Token Maintenance
#
# Checks if the Claude OAuth token is nearing expiry and attempts to refresh it
# before it expires. Sends Telegram alerts if refresh fails.
#
# Install via cron (every 2 hours):
#   0 */2 * * * $HOME/lobster/scripts/token-refresh.sh
#
# How it works:
#   1. Reads expiresAt from ~/.claude/.credentials.json
#   2. If token expires within REFRESH_THRESHOLD (4 hours), attempt refresh
#   3. `claude auth status` triggers Claude Code's internal token refresh
#   4. If refresh fails, alert via Telegram
#===============================================================================

set -o pipefail

CREDS_FILE="$HOME/.claude/.credentials.json"
LOG_FILE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/token-refresh.log"
REFRESH_THRESHOLD=14400  # 4 hours in seconds
CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"

# Consecutive-failure counter: only alert after this many back-to-back
# refresh attempts that all left the token expired or very close to expiry.
# One failed attempt is not enough — Claude refreshes lazily on actual API
# calls, so expiresAt may not change until Claude next makes a real request.
FAILURE_COUNTER_FILE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/token-refresh-failures"
ALERT_COOLDOWN_FILE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/token-refresh-last-alert"
ALERT_AFTER_FAILURES=2   # alert on the 2nd consecutive failed refresh
ALERT_COOLDOWN_SECONDS=43200  # 12-hour cooldown between alerts

# Ensure PATH includes claude
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date -Iseconds)] $1" >> "$LOG_FILE"
}

should_alert() {
    # Returns 0 (true) only if 12 hours have passed since the last alert
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
    if [[ ! -f "$CREDS_FILE" ]]; then
        log "No credentials file found"
        return 0
    fi

    # Get time remaining on token
    local remaining
    remaining=$(python3 -c "
import json, time
try:
    d = json.load(open('$CREDS_FILE'))
    ea = d.get('claudeAiOauth', {}).get('expiresAt', 0) / 1000
    print(f'{ea - time.time():.0f}')
except:
    print('-1')
" 2>/dev/null)

    local remaining_hours=$(( ${remaining:-0} / 3600 ))

    if [[ "${remaining:-0}" -gt "$REFRESH_THRESHOLD" ]]; then
        log "Token healthy: ${remaining_hours}h remaining. No action needed."
        return 0
    fi

    if [[ "${remaining:-0}" -le 0 ]]; then
        # Token appears expired in the file — but Claude may have already refreshed
        # it for its own use (lazy refresh on API call). Only alert after consecutive
        # failures to avoid false positives on transient expiry states.
        local failure_count=0
        if [[ -f "$FAILURE_COUNTER_FILE" ]]; then
            failure_count=$(cat "$FAILURE_COUNTER_FILE" 2>/dev/null || echo 0)
        fi
        failure_count=$((failure_count + 1))
        echo "$failure_count" > "$FAILURE_COUNTER_FILE"
        log "Token EXPIRED in credentials file. Consecutive count: $failure_count (threshold: $ALERT_AFTER_FAILURES)."

        if [[ $failure_count -ge $ALERT_AFTER_FAILURES ]]; then
            log "ALERT: Token confirmed expired after $failure_count consecutive checks."
            if should_alert; then
                date +%s > "$ALERT_COOLDOWN_FILE"
                send_telegram_alert "🔑 *Token Expired*

Claude OAuth token has expired and has not been refreshed after $failure_count consecutive checks.

Fix: SSH in and run \`claude auth login\`"
            fi
            rm -f "$FAILURE_COUNTER_FILE"
        fi
        return 1
    fi

    log "Token expiring soon: ${remaining_hours}h remaining. Attempting refresh..."

    # Attempt refresh by calling claude auth status
    # This triggers Claude Code's internal token refresh logic
    local pre_expires_at
    pre_expires_at=$(python3 -c "
import json
d = json.load(open('$CREDS_FILE'))
print(d.get('claudeAiOauth', {}).get('expiresAt', 0))
" 2>/dev/null)

    # Run auth status (unset CLAUDECODE to avoid nested session error)
    env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude auth status > /dev/null 2>&1

    # Check if expiresAt changed (indicating successful refresh)
    local post_expires_at
    post_expires_at=$(python3 -c "
import json
d = json.load(open('$CREDS_FILE'))
print(d.get('claudeAiOauth', {}).get('expiresAt', 0))
" 2>/dev/null)

    if [[ "$post_expires_at" != "$pre_expires_at" && "${post_expires_at:-0}" -gt "${pre_expires_at:-0}" ]]; then
        local new_remaining
        new_remaining=$(python3 -c "
import json, time
d = json.load(open('$CREDS_FILE'))
ea = d.get('claudeAiOauth', {}).get('expiresAt', 0) / 1000
print(f'{(ea - time.time()) / 3600:.1f}')
" 2>/dev/null)
        log "Token refreshed successfully! New expiry: ${new_remaining}h from now"
        # Successful refresh: reset failure counter
        rm -f "$FAILURE_COUNTER_FILE"
        return 0
    else
        # expiresAt did not change — Claude refreshes lazily on actual API calls,
        # so this is expected and does NOT mean a real failure. Only alert after
        # ALERT_AFTER_FAILURES consecutive runs where the token remains expiring.
        log "Token refresh did not update expiresAt (Claude refreshes lazily on next API call)."

        # Read and increment failure counter
        local failure_count=0
        if [[ -f "$FAILURE_COUNTER_FILE" ]]; then
            failure_count=$(cat "$FAILURE_COUNTER_FILE" 2>/dev/null || echo 0)
        fi
        failure_count=$((failure_count + 1))
        echo "$failure_count" > "$FAILURE_COUNTER_FILE"
        log "Consecutive refresh-unchanged count: $failure_count (alert threshold: $ALERT_AFTER_FAILURES)"

        # Only alert once we've seen this condition ALERT_AFTER_FAILURES times in a row.
        # Also require the token to actually be expired (remaining <= 0), not merely
        # within the 1-hour warning window — if there's still time left, Claude will
        # self-refresh before the next real API call.
        if [[ $failure_count -ge $ALERT_AFTER_FAILURES && "${remaining:-0}" -le 0 ]]; then
            log "ALERT: Token confirmed expired after $failure_count consecutive failed refreshes."
            if should_alert; then
                date +%s > "$ALERT_COOLDOWN_FILE"
                send_telegram_alert "🔑 *Token Refresh Failed*

Claude OAuth token is expired and auto-refresh has not worked after $failure_count attempts.

Fix: SSH in and run \`claude auth login\`"
            fi
            # Reset counter after alerting so we don't spam
            rm -f "$FAILURE_COUNTER_FILE"
        elif [[ $failure_count -ge $ALERT_AFTER_FAILURES && "${remaining:-0}" -lt 3600 ]]; then
            log "ALERT: Token expiring in ${remaining_hours}h and refresh unchanged after $failure_count attempts."
            if should_alert; then
                date +%s > "$ALERT_COOLDOWN_FILE"
                send_telegram_alert "🔑 *Token Refresh Warning*

Claude OAuth token expires in ${remaining_hours}h and auto-refresh has not updated the expiry after $failure_count consecutive attempts.

This may resolve on its own (Claude refreshes lazily). If Claude stops working, run \`claude auth login\`."
            fi
            # Reset counter after alerting so we don't spam
            rm -f "$FAILURE_COUNTER_FILE"
        fi
        return 1
    fi
}

main "$@"
