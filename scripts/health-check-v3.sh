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
#   */2 * * * * $HOME/lobster/scripts/health-check-v3.sh
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
LOBSTER_STATE_FILE="${LOBSTER_STATE_FILE_OVERRIDE:-$HOME/messages/config/lobster-state.json}"
STALE_THRESHOLD_SECONDS=180          # 3 minutes - RED if any message older (watchdog handles soft recovery at 90s)
YELLOW_THRESHOLD_SECONDS=120         # 2 minutes - YELLOW warning

LOG_FILE="$HOME/lobster-workspace/logs/health-check.log"
LOCK_FILE="/tmp/lobster-health-check-v3.lock"

MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN_SECONDS=600         # 10 min window for counting attempts
RESTART_STATE_FILE="$HOME/lobster-workspace/logs/health-restart-state-v3"

MEMORY_THRESHOLD=90                  # percentage
DISK_THRESHOLD=95                    # percentage

# User-facing message sources (only these count for inbox staleness)
USER_FACING_SOURCES="telegram sms signal slack"

# Circuit breaker: tracks which stale files already triggered a restart
# to prevent restart loops when the same message persists after restart
STALE_INBOX_MARKER_DIR="$HOME/lobster-workspace/logs/stale-inbox-markers"

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
# Hibernation State Check
#===============================================================================

# Read the current Lobster mode from state file.
# Returns 0 (exit code) if mode is "hibernate", 1 if "active" or unknown.
read_lobster_mode() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        echo "active"
        return
    fi
    python3 -c "
import json, sys
try:
    d = json.load(open('$LOBSTER_STATE_FILE'))
    print(d.get('mode', 'active'))
except Exception:
    print('active')
" 2>/dev/null || echo "active"
}

is_hibernating() {
    local mode
    mode=$(read_lobster_mode)
    [[ "$mode" == "hibernate" ]]
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

# Check if a source is user-facing (should count toward inbox staleness)
is_user_facing_source() {
    local source="$1"
    local s
    for s in $USER_FACING_SOURCES; do
        [[ "$source" == "$s" ]] && return 0
    done
    return 1
}

# Check 4: Inbox drain - THE primary deterministic check
# Only counts messages from user-facing sources (telegram, sms, signal, slack).
# System/internal/task-output messages are ignored - they may sit in the inbox
# legitimately without indicating a stuck system.
#
# Circuit breaker: if a stale file already triggered a restart (tracked via
# marker files), it is skipped to prevent restart loops.
#
# Returns: 0=GREEN, 1=YELLOW, 2=RED
check_inbox_drain() {
    local now
    now=$(date +%s)
    local oldest_age=0
    local stale_count=0
    local yellow_count=0
    local total_count=0
    local skipped_system=0
    local skipped_circuit_breaker=0

    while IFS= read -r -d '' f; do
        local basename_f
        basename_f=$(basename "$f")

        # Parse source from JSON using jq; skip if unparseable or missing
        local source
        source=$(jq -r '.source // empty' "$f" 2>/dev/null)
        if [[ -z "$source" ]]; then
            log_info "Skipping $basename_f: cannot parse source field"
            continue
        fi

        # Only count user-facing sources
        if ! is_user_facing_source "$source"; then
            skipped_system=$((skipped_system + 1))
            continue
        fi

        # Circuit breaker: skip files that already triggered a restart
        if [[ -d "$STALE_INBOX_MARKER_DIR" && -f "$STALE_INBOX_MARKER_DIR/$basename_f" ]]; then
            skipped_circuit_breaker=$((skipped_circuit_breaker + 1))
            log_info "Circuit breaker: skipping $basename_f (already triggered restart)"
            continue
        fi

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

    if [[ $skipped_system -gt 0 ]]; then
        log_info "Inbox drain: skipped $skipped_system non-user message(s)"
    fi
    if [[ $skipped_circuit_breaker -gt 0 ]]; then
        log_info "Inbox drain: skipped $skipped_circuit_breaker circuit-breaker message(s)"
    fi

    if [[ $stale_count -gt 0 ]]; then
        log_error "RED: $stale_count user message(s) older than ${STALE_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 2
    elif [[ $yellow_count -gt 0 ]]; then
        log_warn "YELLOW: $yellow_count user message(s) older than ${YELLOW_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 1
    elif [[ $total_count -gt 0 ]]; then
        log_info "Inbox has $total_count user message(s), all fresh (oldest: ${oldest_age}s)"
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
# Circuit Breaker - prevent restart loops for persistent stale messages
#===============================================================================

# Record which inbox files triggered a stale-inbox restart.
# On the next health check, these files will be skipped by check_inbox_drain()
# so we don't restart again for the same stuck messages.
record_stale_inbox_markers() {
    mkdir -p "$STALE_INBOX_MARKER_DIR"
    # Clear old markers first
    rm -f "$STALE_INBOX_MARKER_DIR"/*.json 2>/dev/null

    local now
    now=$(date +%s)

    while IFS= read -r -d '' f; do
        local basename_f
        basename_f=$(basename "$f")
        local source
        source=$(jq -r '.source // empty' "$f" 2>/dev/null)
        [[ -z "$source" ]] && continue
        is_user_facing_source "$source" || continue

        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))
        if [[ $age -gt $STALE_THRESHOLD_SECONDS ]]; then
            touch "$STALE_INBOX_MARKER_DIR/$basename_f"
            log_info "Circuit breaker: marked $basename_f as restart-triggering"
        fi
    done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)
}

# Clear circuit breaker markers (called when inbox is healthy)
clear_stale_inbox_markers() {
    if [[ -d "$STALE_INBOX_MARKER_DIR" ]]; then
        rm -rf "$STALE_INBOX_MARKER_DIR"
    fi
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

    # If restarting for stale inbox, record which files triggered it
    # so the circuit breaker can skip them on the next check
    if [[ "$reason" == *"stale inbox"* ]]; then
        record_stale_inbox_markers
    fi

    record_restart

    # Restart via systemd - this handles tmux lifecycle correctly
    sudo systemctl restart "$SERVICE_CLAUDE" 2>&1 | while read -r line; do
        log_info "systemctl: $line"
    done

    # Wait for startup
    sleep 5

    # Verify recovery: service and tmux must be running
    if systemctl is-active --quiet "$SERVICE_CLAUDE" 2>/dev/null && \
       tmux -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then

        # For stale-inbox restarts, also re-verify inbox drain
        if [[ "$reason" == *"stale inbox"* ]]; then
            # Re-check inbox (circuit breaker markers will skip already-known files)
            check_inbox_drain
            local post_rc=$?
            if [[ $post_rc -eq 2 ]]; then
                log_warn "Post-restart: inbox still has NEW stale messages (not same as pre-restart)"
                send_telegram_alert "System restarted but inbox still has stale messages.

Reason: $reason
Status: Restarted, but new stale messages detected post-restart"
                return 0
            fi
        fi

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

    # --- Hibernation guard: skip Claude restart but still check router ---
    local _is_hibernating=false
    if is_hibernating; then
        _is_hibernating=true
        log_info "HIBERNATE: Lobster is in hibernate mode - will skip Claude restart but still check router"
    fi

    # --- Infrastructure checks (RED if any fail) ---

    # Always check systemd services (includes router/bot) — even when hibernating
    if ! check_services; then
        level="RED"
        restart_reason="systemd service not active"
    fi

    # Skip Claude-specific checks when hibernating (Claude intentionally exited)
    if [[ "$_is_hibernating" == "true" ]]; then
        log_info "HIBERNATE: Skipping Claude process, tmux, and inbox drain checks"
    else
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
        elif [[ $inbox_rc -eq 0 ]]; then
            # Inbox is healthy - clear any circuit breaker markers
            clear_stale_inbox_markers
        fi
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
