#!/bin/bash
#===============================================================================
# Lobster Health Check v3 - Lifecycle-Aware, Deterministic Monitoring
#
# Design principles:
#   - Zero LLM dependency: no heartbeat, no tmux scraping, no MCP checks
#   - Lifecycle-aware: reads lobster-state.json to understand current phase
#   - Single observable truth: is the inbox draining?
#   - Recovery via systemd: never manually rebuild tmux sessions
#   - Direct Telegram alerts: curl, not outbox (outbox may be broken too)
#   - Low noise: only alert on genuine problems, not routine transitions
#
# Lifecycle states (from claude-persistent.sh):
#   active     - Claude is running (WAITING on wait_for_messages or PROCESSING)
#   starting   - Wrapper is launching Claude (transient, < 30s)
#   restarting - Wrapper is restarting after an exit (transient, < 60s)
#   hibernate  - Claude exited cleanly, wrapper watching for inbox messages
#   backoff    - Wrapper hit rapid-restart limit, cooling down
#   stopped    - Wrapper received signal, shutting down
#   waking     - Wrapper detected messages, about to launch Claude
#
# Escalation ladder:
#   GREEN  - All checks pass (or in expected transient state)
#   YELLOW - Inbox messages exist < STALE threshold, or transient state
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

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"

INBOX_DIR="$MESSAGES_DIR/inbox"
LOBSTER_STATE_FILE="${LOBSTER_STATE_FILE_OVERRIDE:-$MESSAGES_DIR/config/lobster-state.json}"
STALE_THRESHOLD_SECONDS=180          # 3 minutes - RED if any message older (watchdog handles soft recovery at 90s)
YELLOW_THRESHOLD_SECONDS=120         # 2 minutes - YELLOW warning

OUTBOX_DIR="$MESSAGES_DIR/outbox"
OUTBOX_STALE_THRESHOLD_SECONDS=900   # 15 min = RED
OUTBOX_YELLOW_THRESHOLD_SECONDS=300  # 5 min = YELLOW
OUTBOX_HISTORICAL_CUTOFF=3600        # Skip files > 1 hour (dead-letter candidates)

LOG_FILE="$WORKSPACE_DIR/logs/health-check.log"
LOCK_FILE="/tmp/lobster-health-check-v3.lock"

MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN_SECONDS=600         # 10 min window for counting attempts
RESTART_STATE_FILE="$WORKSPACE_DIR/logs/health-restart-state-v3"

MEMORY_THRESHOLD=90                  # percentage
DISK_THRESHOLD=95                    # percentage

# User-facing message sources (only these count for inbox staleness)
USER_FACING_SOURCES="telegram sms signal slack"

# Circuit breaker: tracks which stale files already triggered a restart
# to prevent restart loops when the same message persists after restart
STALE_INBOX_MARKER_DIR="$WORKSPACE_DIR/logs/stale-inbox-markers"

# Telegram direct alerting (bypasses outbox entirely)
CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"

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
# Lifecycle State Check
#===============================================================================

# Read the current Lobster mode from state file.
# Returns one of: active, starting, restarting, hibernate, backoff, stopped, waking, unknown
read_lobster_mode() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        echo "unknown"
        return
    fi
    python3 -c "
import json, sys
try:
    d = json.load(open('$LOBSTER_STATE_FILE'))
    print(d.get('mode', 'unknown'))
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

# Read state file age in seconds
read_state_age() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        echo "999999"
        return
    fi
    local file_time
    file_time=$(stat -c %Y "$LOBSTER_STATE_FILE" 2>/dev/null)
    if [[ -z "$file_time" ]]; then
        echo "999999"
        return
    fi
    local now
    now=$(date +%s)
    echo $((now - file_time))
}

is_hibernating() {
    local mode
    mode=$(read_lobster_mode)
    [[ "$mode" == "hibernate" ]]
}

# Check if the wrapper script (claude-persistent.sh) is running in tmux.
# The wrapper manages Claude's lifecycle, so if it's running, the system
# is operational even if Claude is temporarily absent (between restarts,
# hibernating, etc.)
check_wrapper_process() {
    local wrapper_pids
    wrapper_pids=$(pgrep -f "claude-persistent.sh" 2>/dev/null)

    if [[ -z "$wrapper_pids" ]]; then
        return 1
    fi

    # Verify at least one wrapper is in the lobster tmux
    local tmux_panes
    tmux_panes=$(tmux -L "$TMUX_SOCKET" list-panes -t "$TMUX_SESSION" -F '#{pane_pid}' 2>/dev/null)
    [[ -z "$tmux_panes" ]] && return 1

    for pid in $wrapper_pids; do
        local check_pid="$pid"
        for _ in 1 2 3 4 5 6; do
            if echo "$tmux_panes" | grep -qw "$check_pid"; then
                return 0
            fi
            check_pid=$(ps -o ppid= -p "$check_pid" 2>/dev/null | tr -d ' ')
            [[ -z "$check_pid" || "$check_pid" == "1" ]] && break
        done
    done

    return 1
}

# Transient states where Claude may not be running but everything is fine
is_transient_state() {
    local mode="$1"
    local age="$2"
    # Starting/restarting/waking are transient - allow up to 120s
    case "$mode" in
        starting|restarting|waking)
            [[ $age -lt 120 ]]
            return $?
            ;;
        backoff)
            # Backoff is expected - allow up to 300s (5 min)
            [[ $age -lt 300 ]]
            return $?
            ;;
        *)
            return 1
            ;;
    esac
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

# Check 5: Outbox drain - are outgoing messages being delivered?
# Returns: 0=GREEN, 1=YELLOW, 2=RED
check_outbox_drain() {
    local now
    now=$(date +%s)
    local oldest_age=0
    local stale_count=0
    local yellow_count=0
    local total_count=0

    while IFS= read -r -d '' f; do
        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))

        # Skip historical stuck files (dead-letter candidates)
        [[ $age -gt $OUTBOX_HISTORICAL_CUTOFF ]] && continue

        total_count=$((total_count + 1))
        [[ $age -gt $oldest_age ]] && oldest_age=$age

        if [[ $age -gt $OUTBOX_STALE_THRESHOLD_SECONDS ]]; then
            stale_count=$((stale_count + 1))
        elif [[ $age -gt $OUTBOX_YELLOW_THRESHOLD_SECONDS ]]; then
            yellow_count=$((yellow_count + 1))
        fi
    done < <(find "$OUTBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    if [[ $stale_count -gt 0 ]]; then
        log_error "RED: $stale_count outbox message(s) older than ${OUTBOX_STALE_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 2
    elif [[ $yellow_count -gt 0 ]]; then
        log_warn "YELLOW: $yellow_count outbox message(s) older than ${OUTBOX_YELLOW_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 1
    elif [[ $total_count -gt 0 ]]; then
        log_info "Outbox has $total_count message(s), all fresh (oldest: ${oldest_age}s)"
        return 0
    else
        return 0
    fi
}

# Check 6: Memory
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

# Check 7: Disk
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

    # --- Read lifecycle state ---
    local lobster_mode
    lobster_mode=$(read_lobster_mode)
    local state_age
    state_age=$(read_state_age)
    log_info "Lifecycle state: mode=$lobster_mode, state_age=${state_age}s"

    # --- Always check systemd services (includes router/bot) ---
    if ! check_services; then
        level="RED"
        restart_reason="systemd service not active"
    fi

    # --- Lifecycle-aware Claude checks ---
    #
    # The persistent wrapper (claude-persistent.sh) manages Claude's lifecycle.
    # We need to check differently depending on the current phase:
    #
    # hibernate:  Claude exited cleanly, wrapper is polling for new messages.
    #             No Claude process expected. Only alert if stale user messages.
    # active:     Claude should be running. Full checks apply.
    # starting/restarting/waking: Transient states. Wrapper is handling it.
    # backoff:    Wrapper hit rapid-restart limit. Expected pause.
    # stopped:    Wrapper was stopped. Systemd should restart.
    # unknown:    No state file. Either first run or old-style wrapper.
    #

    case "$lobster_mode" in
        hibernate)
            log_info "HIBERNATE: Claude cleanly exited. Wrapper polling for new messages."

            # In hibernate mode, check if the wrapper is still running
            if ! check_tmux; then
                level="RED"
                restart_reason="tmux session missing (hibernate mode)"
            elif ! check_wrapper_process; then
                # Wrapper died during hibernation — need systemd restart
                level="RED"
                restart_reason="wrapper process missing during hibernation"
            fi

            # Still check inbox: if user messages are sitting stale, the wrapper
            # should have woken Claude by now. Give extra time (5 min) since
            # the wrapper polls every 10s.
            check_inbox_drain
            local hibernate_inbox_rc=$?
            if [[ $hibernate_inbox_rc -eq 2 ]]; then
                # Stale user messages during hibernation — wrapper may be stuck
                level="RED"
                restart_reason="${restart_reason:+$restart_reason + }stale inbox during hibernation"
            fi
            ;;

        starting|restarting|waking)
            # Transient states — allow some time before alarming
            if is_transient_state "$lobster_mode" "$state_age"; then
                log_info "TRANSIENT: mode=$lobster_mode for ${state_age}s — within expected window"
                # Don't check for Claude process during transient states
            else
                log_warn "STALE TRANSIENT: mode=$lobster_mode for ${state_age}s — exceeded expected window"
                if ! check_tmux; then
                    level="RED"
                    restart_reason="tmux session missing (stale $lobster_mode state)"
                elif ! check_wrapper_process; then
                    level="RED"
                    restart_reason="wrapper process missing (stale $lobster_mode state)"
                fi
            fi

            # Still check inbox drain
            check_inbox_drain
            local transient_inbox_rc=$?
            if [[ $transient_inbox_rc -eq 2 ]]; then
                level="RED"
                restart_reason="${restart_reason:+$restart_reason + }stale inbox (>$((STALE_THRESHOLD_SECONDS/60))m)"
            elif [[ $transient_inbox_rc -eq 1 && "$level" == "GREEN" ]]; then
                level="YELLOW"
            elif [[ $transient_inbox_rc -eq 0 ]]; then
                clear_stale_inbox_markers
            fi
            ;;

        backoff)
            if is_transient_state "$lobster_mode" "$state_age"; then
                log_info "BACKOFF: Wrapper cooling down (${state_age}s) — expected behavior"
            else
                log_warn "EXTENDED BACKOFF: ${state_age}s — may need intervention"
                if [[ "$level" == "GREEN" ]]; then
                    level="YELLOW"
                fi
            fi

            # Check inbox drain even during backoff
            check_inbox_drain
            local backoff_inbox_rc=$?
            if [[ $backoff_inbox_rc -eq 2 ]]; then
                level="RED"
                restart_reason="${restart_reason:+$restart_reason + }stale inbox during backoff"
            elif [[ $backoff_inbox_rc -eq 0 ]]; then
                clear_stale_inbox_markers
            fi
            ;;

        stopped)
            # Wrapper was intentionally stopped — systemd should catch this
            log_warn "STOPPED: Wrapper received shutdown signal"
            # Let systemd handle restart; don't duplicate
            ;;

        active|unknown|*)
            # Standard checks: wrapper + Claude should be running
            if ! check_tmux; then
                level="RED"
                restart_reason="tmux session missing"
            fi

            # In persistent mode, check for wrapper OR Claude process
            # The wrapper is always running; Claude may be temporarily absent
            # during restarts, but the wrapper handles that.
            local has_wrapper=false
            local has_claude=false

            if check_wrapper_process; then
                has_wrapper=true
            fi
            if check_claude_process; then
                has_claude=true
            fi

            if [[ "$has_wrapper" == "false" && "$has_claude" == "false" ]]; then
                level="RED"
                restart_reason="${restart_reason:+$restart_reason + }no wrapper or Claude process in lobster tmux"
            elif [[ "$has_wrapper" == "false" && "$has_claude" == "true" ]]; then
                # Claude running without wrapper — old-style or something unexpected
                # Not critical, but worth noting
                log_warn "Claude running without persistent wrapper (old-style mode?)"
            elif [[ "$has_wrapper" == "true" && "$has_claude" == "false" ]]; then
                # Wrapper running but no Claude — could be between launches
                # Check state age: if it's been a while, something may be stuck
                if [[ $state_age -gt 120 && "$lobster_mode" == "active" ]]; then
                    log_warn "Wrapper running but no Claude for ${state_age}s in active state"
                    if [[ "$level" == "GREEN" ]]; then
                        level="YELLOW"
                    fi
                else
                    log_info "Wrapper running, Claude temporarily absent (state: $lobster_mode, age: ${state_age}s)"
                fi
            fi

            # Inbox drain check
            check_inbox_drain
            local inbox_rc=$?
            if [[ $inbox_rc -eq 2 ]]; then
                level="RED"
                restart_reason="${restart_reason:+$restart_reason + }stale inbox (>$((STALE_THRESHOLD_SECONDS/60))m)"
            elif [[ $inbox_rc -eq 1 && "$level" == "GREEN" ]]; then
                level="YELLOW"
            elif [[ $inbox_rc -eq 0 ]]; then
                clear_stale_inbox_markers
            fi
            ;;
    esac

    # --- Outbox drain check (are replies being delivered?) ---

    check_outbox_drain
    local outbox_rc=$?
    if [[ $outbox_rc -eq 2 ]]; then
        level="RED"
        restart_reason="${restart_reason:+$restart_reason + }stale outbox (>$((OUTBOX_STALE_THRESHOLD_SECONDS/60))m)"
    elif [[ $outbox_rc -eq 1 && "$level" == "GREEN" ]]; then
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
            log_info "GREEN: All checks passed (mode=$lobster_mode)"
            ;;
        YELLOW)
            log_warn "YELLOW: Non-critical issues detected (mode=$lobster_mode), monitoring"
            ;;
        RED)
            log_error "RED: Critical failure (mode=$lobster_mode) - $restart_reason"
            do_restart "$restart_reason"
            ;;
    esac

    log_info "=== Health check v3 complete (level=$level, mode=$lobster_mode) ==="
}

main "$@"
