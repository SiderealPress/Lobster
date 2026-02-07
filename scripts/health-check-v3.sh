#!/bin/bash
#===============================================================================
# Lobster Health Check v3 - Deterministic, LLM-Independent Monitoring
#
# Design principles:
#   - Zero LLM dependency: no heartbeat, no tmux scraping, no MCP checks
#   - Single observable truth: is the inbox draining?
#   - Recovery via systemd: never manually rebuild tmux sessions
#   - Direct Telegram alerts: curl, not outbox (outbox may be broken too)
#
# Escalation ladder:
#   GREEN  - All checks pass
#   YELLOW - Inbox messages exist < STALE threshold
#   RED    - Stale inbox > threshold OR missing process/tmux/service → restart
#   BLACK  - 3 restart failures in cooldown window → alert, stop retrying
#
# Run via cron every 2 minutes:
#   */2 * * * * /home/admin/lobster/scripts/health-check-v3.sh
#===============================================================================

set -o pipefail

#===============================================================================
# Configuration - single source of truth
#===============================================================================
TMUX_SOCKET="lobster"
TMUX_SESSION="lobster"
SERVICE_CLAUDE="lobster-claude"
SERVICE_ROUTER="lobster-router"

INBOX_DIR="$HOME/messages/inbox"
STALE_THRESHOLD_SECONDS=300          # 5 minutes - RED if any message older
YELLOW_THRESHOLD_SECONDS=120         # 2 minutes - YELLOW warning

LOG_FILE="$HOME/lobster-workspace/logs/health-check.log"
LOCK_FILE="/tmp/lobster-health-check-v3.lock"

MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN_SECONDS=600         # 10 min window for counting attempts
RESTART_STATE_FILE="$HOME/lobster-workspace/logs/health-restart-state-v3"

MEMORY_THRESHOLD=90                  # percentage
DISK_THRESHOLD=95                    # percentage

# Telegram direct alerting (bypasses outbox entirely)
CONFIG_ENV="$HOME/lobster/config/config.env"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$RESTART_STATE_FILE")"

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
# Locking - prevent concurrent health checks
#===============================================================================
acquire_lock() {
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        exit 0
    fi
}

#===============================================================================
# Direct Telegram Alert (no LLM, no outbox, no MCP)
#===============================================================================
send_telegram_alert() {
    local message="$1"

    # Source config.env for bot token and user ID
    local bot_token=""
    local chat_id=""

    if [[ -f "$CONFIG_ENV" ]]; then
        bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$CONFIG_ENV" | cut -d'=' -f2-)
        chat_id=$(grep '^TELEGRAM_ALLOWED_USERS=' "$CONFIG_ENV" | cut -d'=' -f2- | cut -d',' -f1)
    fi

    if [[ -z "$bot_token" || -z "$chat_id" ]]; then
        log_error "Cannot send Telegram alert: missing bot token or chat ID"
        return 1
    fi

    local full_message="🚨 *Lobster Health Alert*

${message}

_$(date '+%Y-%m-%d %H:%M:%S %Z')_"

    curl -s -X POST \
        "https://api.telegram.org/bot${bot_token}/sendMessage" \
        -d chat_id="$chat_id" \
        -d text="$full_message" \
        -d parse_mode="Markdown" \
        --max-time 10 \
        > /dev/null 2>&1

    local rc=$?
    if [[ $rc -eq 0 ]]; then
        log_info "Telegram alert sent to $chat_id"
    else
        log_error "Telegram alert failed (curl exit $rc)"
    fi
}

#===============================================================================
# Restart Rate Limiting
#===============================================================================
can_restart() {
    if [[ ! -f "$RESTART_STATE_FILE" ]]; then
        return 0
    fi

    read -r last_restart_time restart_count < "$RESTART_STATE_FILE" 2>/dev/null || return 0
    local now
    now=$(date +%s)
    local elapsed=$((now - last_restart_time))

    # Reset counter if cooldown has fully passed
    if [[ $elapsed -gt $RESTART_COOLDOWN_SECONDS ]]; then
        return 0
    fi

    # Check if we've exceeded max attempts within the window
    if [[ $restart_count -ge $MAX_RESTART_ATTEMPTS ]]; then
        return 1
    fi

    return 0
}

record_restart() {
    local now
    now=$(date +%s)
    local restart_count=0

    if [[ -f "$RESTART_STATE_FILE" ]]; then
        read -r last_restart_time restart_count < "$RESTART_STATE_FILE" 2>/dev/null
        local elapsed=$((now - last_restart_time))
        if [[ $elapsed -gt $RESTART_COOLDOWN_SECONDS ]]; then
            restart_count=0
        fi
    fi

    restart_count=$((restart_count + 1))
    echo "$now $restart_count" > "$RESTART_STATE_FILE"
}

#===============================================================================
# Health Checks - all deterministic, no LLM dependency
#===============================================================================

# Check 1: Are systemd services active?
check_services() {
    local failed=0

    if ! systemctl is-active --quiet "$SERVICE_CLAUDE" 2>/dev/null; then
        log_error "Service $SERVICE_CLAUDE is not active"
        failed=1
    fi

    if ! systemctl is-active --quiet "$SERVICE_ROUTER" 2>/dev/null; then
        log_error "Service $SERVICE_ROUTER is not active"
        failed=1
    fi

    return $failed
}

# Check 2: Does the tmux session exist?
check_tmux() {
    if tmux -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
        return 0
    else
        log_error "Tmux session '$TMUX_SESSION' on socket '$TMUX_SOCKET' not found"
        return 1
    fi
}

# Check 3: Is a Claude process running inside the lobster tmux?
check_claude_process() {
    local claude_pids
    claude_pids=$(pgrep -f "claude.*--dangerously-skip-permissions" 2>/dev/null)

    if [[ -z "$claude_pids" ]]; then
        log_error "No Claude process found"
        return 1
    fi

    # Verify at least one Claude process is a descendant of the tmux session
    local tmux_panes
    tmux_panes=$(tmux -L "$TMUX_SOCKET" list-panes -t "$TMUX_SESSION" -F '#{pane_pid}' 2>/dev/null)

    if [[ -z "$tmux_panes" ]]; then
        log_error "Cannot list tmux panes"
        return 1
    fi

    for pid in $claude_pids; do
        local check_pid="$pid"
        # Walk up to 6 levels of parent PIDs
        for _ in 1 2 3 4 5 6; do
            if echo "$tmux_panes" | grep -qw "$check_pid"; then
                log_info "Claude PID $pid is in lobster tmux (ancestor $check_pid matches pane)"
                return 0
            fi
            check_pid=$(ps -o ppid= -p "$check_pid" 2>/dev/null | tr -d ' ')
            [[ -z "$check_pid" || "$check_pid" == "1" ]] && break
        done
    done

    log_error "Claude process(es) found but none are in the lobster tmux session"
    return 1
}

# Check 4: Inbox drain - THE primary deterministic check
# Returns: 0=GREEN, 1=YELLOW, 2=RED
check_inbox_drain() {
    local now
    now=$(date +%s)
    local oldest_age=0
    local stale_count=0
    local yellow_count=0
    local total_count=0

    while IFS= read -r -d '' f; do
        total_count=$((total_count + 1))
        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))
        [[ $age -gt $oldest_age ]] && oldest_age=$age

        if [[ $age -gt $STALE_THRESHOLD_SECONDS ]]; then
            stale_count=$((stale_count + 1))
        elif [[ $age -gt $YELLOW_THRESHOLD_SECONDS ]]; then
            yellow_count=$((yellow_count + 1))
        fi
    done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    if [[ $stale_count -gt 0 ]]; then
        log_error "RED: $stale_count message(s) older than ${STALE_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 2
    elif [[ $yellow_count -gt 0 ]]; then
        log_warn "YELLOW: $yellow_count message(s) older than ${YELLOW_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 1
    elif [[ $total_count -gt 0 ]]; then
        log_info "Inbox has $total_count message(s), all fresh (oldest: ${oldest_age}s)"
        return 0
    else
        return 0
    fi
}

# Check 5: Memory
check_memory() {
    local mem_pct
    mem_pct=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2 * 100}')

    if [[ $mem_pct -gt $MEMORY_THRESHOLD ]]; then
        log_error "Memory critical: ${mem_pct}% (threshold: ${MEMORY_THRESHOLD}%)"
        return 1
    fi

    log_info "Memory OK: ${mem_pct}%"
    return 0
}

# Check 6: Disk
check_disk() {
    local disk_pct
    disk_pct=$(df "$HOME" | awk 'NR==2 {gsub(/%/,""); print $5}')

    if [[ $disk_pct -gt $DISK_THRESHOLD ]]; then
        log_error "Disk critical: ${disk_pct}% (threshold: ${DISK_THRESHOLD}%)"
        return 1
    fi

    log_info "Disk OK: ${disk_pct}%"
    return 0
}

#===============================================================================
# Recovery - always via systemd, never manual tmux
#===============================================================================
do_restart() {
    local reason="$1"
    log_warn "Restarting $SERVICE_CLAUDE (reason: $reason)"

    if ! can_restart; then
        log_error "BLACK: Max restart attempts ($MAX_RESTART_ATTEMPTS) in ${RESTART_COOLDOWN_SECONDS}s window"
        send_telegram_alert "System unrecoverable after $MAX_RESTART_ATTEMPTS restart attempts.

Reason: $reason

Manual intervention required:
\`lobster restart\`"
        return 1
    fi

    record_restart

    # Restart via systemd - this handles tmux lifecycle correctly
    sudo systemctl restart "$SERVICE_CLAUDE" 2>&1 | while read -r line; do
        log_info "systemctl: $line"
    done

    # Wait for startup
    sleep 5

    # Verify recovery
    if systemctl is-active --quiet "$SERVICE_CLAUDE" 2>/dev/null && \
       tmux -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
        log_info "Restart successful"
        send_telegram_alert "System recovered automatically.

Reason: $reason
Status: Restarted successfully"
        return 0
    else
        log_error "Restart verification failed"
        return 1
    fi
}

#===============================================================================
# Main
#===============================================================================
main() {
    acquire_lock
    log_info "=== Health check v3 starting ==="

    local level="GREEN"
    local restart_reason=""

    # --- Infrastructure checks (RED if any fail) ---

    if ! check_services; then
        level="RED"
        restart_reason="systemd service not active"
    fi

    if ! check_tmux; then
        level="RED"
        restart_reason="tmux session missing"
    fi

    if ! check_claude_process; then
        level="RED"
        restart_reason="no Claude process in lobster tmux"
    fi

    # --- Inbox drain check (overrides to RED if stale) ---

    check_inbox_drain
    local inbox_rc=$?
    if [[ $inbox_rc -eq 2 ]]; then
        level="RED"
        restart_reason="${restart_reason:+$restart_reason + }stale inbox (>$((STALE_THRESHOLD_SECONDS/60))m)"
    elif [[ $inbox_rc -eq 1 && "$level" == "GREEN" ]]; then
        level="YELLOW"
    fi

    # --- Resource checks (RED if critical) ---

    if ! check_memory; then
        level="RED"
        restart_reason="${restart_reason:+$restart_reason + }memory critical"
    fi

    if ! check_disk; then
        # Disk full is not fixable by restart, just alert
        if [[ "$level" != "RED" ]]; then
            level="YELLOW"
        fi
        log_warn "Disk space low - restart won't help, needs manual cleanup"
    fi

    # --- Act on level ---

    case "$level" in
        GREEN)
            log_info "GREEN: All checks passed"
            ;;
        YELLOW)
            log_warn "YELLOW: Non-critical issues detected, monitoring"
            ;;
        RED)
            log_error "RED: Critical failure - $restart_reason"
            do_restart "$restart_reason"
            ;;
    esac

    log_info "=== Health check v3 complete (level=$level) ==="
}

main "$@"
