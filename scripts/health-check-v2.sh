#!/bin/bash
#===============================================================================
# Hyperion Health Check v2 - Robust, LLM-Independent Monitoring
#
# Detects and auto-recovers from:
# 1. Claude process not running
# 2. Claude process running but stuck (not responding)
# 3. Stale messages in inbox not being processed
# 4. MCP server failures
# 5. Memory/resource exhaustion
#
# Run via cron every 2 minutes: */2 * * * * ~/hyperion/scripts/health-check-v2.sh
#===============================================================================

set -o pipefail

# Configuration
HEARTBEAT_FILE="$HOME/hyperion-workspace/logs/claude-heartbeat"
HEARTBEAT_MAX_AGE_SECONDS=600  # 10 minutes - Claude should heartbeat more often
INBOX_DIR="$HOME/messages/inbox"
STALE_MESSAGE_THRESHOLD_MINUTES=15
LOG_FILE="$HOME/hyperion-workspace/logs/health-check.log"
LOCK_FILE="/tmp/hyperion-health-check.lock"
MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN_SECONDS=300  # 5 minutes between restart attempts
RESTART_STATE_FILE="$HOME/hyperion-workspace/logs/health-restart-state"
TMUX_SOCKET="hyperion"
SESSION_NAME="hyperion"

# Memory threshold (percentage)
MEMORY_THRESHOLD=90

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$RESTART_STATE_FILE")"

#-------------------------------------------------------------------------------
# Logging
#-------------------------------------------------------------------------------
log() {
    local level="$1"
    local message="$2"
    echo "[$(date -Iseconds)] [$level] $message" >> "$LOG_FILE"
}

log_info() { log "INFO" "$1"; }
log_warn() { log "WARN" "$1"; }
log_error() { log "ERROR" "$1"; }

#-------------------------------------------------------------------------------
# Locking - prevent concurrent health checks
#-------------------------------------------------------------------------------
acquire_lock() {
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        log_info "Another health check is running, exiting"
        exit 0
    fi
}

#-------------------------------------------------------------------------------
# Restart Rate Limiting
#-------------------------------------------------------------------------------
can_restart() {
    if [[ ! -f "$RESTART_STATE_FILE" ]]; then
        echo "0 0" > "$RESTART_STATE_FILE"
        return 0
    fi

    read -r last_restart_time restart_count < "$RESTART_STATE_FILE"
    local now=$(date +%s)
    local time_since_restart=$((now - last_restart_time))

    # Reset counter if cooldown has passed
    if [[ $time_since_restart -gt $RESTART_COOLDOWN_SECONDS ]]; then
        echo "$now 0" > "$RESTART_STATE_FILE"
        return 0
    fi

    # Check if we've exceeded max attempts
    if [[ $restart_count -ge $MAX_RESTART_ATTEMPTS ]]; then
        log_error "Max restart attempts ($MAX_RESTART_ATTEMPTS) reached in cooldown period"
        return 1
    fi

    return 0
}

record_restart() {
    local now=$(date +%s)
    local restart_count=0

    if [[ -f "$RESTART_STATE_FILE" ]]; then
        read -r last_restart_time restart_count < "$RESTART_STATE_FILE"
        local time_since_restart=$((now - last_restart_time))

        if [[ $time_since_restart -gt $RESTART_COOLDOWN_SECONDS ]]; then
            restart_count=0
        fi
    fi

    restart_count=$((restart_count + 1))
    echo "$now $restart_count" > "$RESTART_STATE_FILE"
}

#-------------------------------------------------------------------------------
# Health Checks
#-------------------------------------------------------------------------------

# Check 1: Is the tmux session running?
check_tmux_session() {
    if tmux -L "$TMUX_SOCKET" has-session -t "$SESSION_NAME" 2>/dev/null; then
        return 0
    else
        log_error "Tmux session '$SESSION_NAME' is not running"
        return 1
    fi
}

# Check 2: Is the Claude process alive?
check_claude_process() {
    local claude_pid=$(pgrep -f "claude.*--dangerously-skip-permissions" | head -1)

    if [[ -z "$claude_pid" ]]; then
        log_error "No Claude process found"
        return 1
    fi

    # Verify process is in the right tmux session
    local process_info=$(ps -p "$claude_pid" -o pid,stat,etime --no-headers 2>/dev/null)
    if [[ -z "$process_info" ]]; then
        log_error "Claude process $claude_pid not responding to ps"
        return 1
    fi

    log_info "Claude process $claude_pid is running: $process_info"
    return 0
}

# Check 3: Is Claude responding? (heartbeat check)
check_heartbeat() {
    if [[ ! -f "$HEARTBEAT_FILE" ]]; then
        log_warn "No heartbeat file found at $HEARTBEAT_FILE"
        # Don't fail on missing heartbeat - it might not be set up yet
        return 0
    fi

    local heartbeat_time=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null)
    local now=$(date +%s)
    local age=$((now - heartbeat_time))

    if [[ $age -gt $HEARTBEAT_MAX_AGE_SECONDS ]]; then
        log_error "Heartbeat is stale: ${age}s old (threshold: ${HEARTBEAT_MAX_AGE_SECONDS}s)"
        return 1
    fi

    log_info "Heartbeat OK: ${age}s old"
    return 0
}

# Check 4: Are there stale messages in inbox?
check_stale_messages() {
    local stale_count=0
    local now=$(date +%s)
    local threshold_seconds=$((STALE_MESSAGE_THRESHOLD_MINUTES * 60))

    # Use find instead of glob to handle empty directories
    while IFS= read -r -d '' f; do
        local file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))

        if [[ $age -gt $threshold_seconds ]]; then
            stale_count=$((stale_count + 1))
            log_warn "Stale message: $f (${age}s old)"
        fi
    done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    if [[ $stale_count -gt 0 ]]; then
        log_error "$stale_count stale message(s) detected (older than ${STALE_MESSAGE_THRESHOLD_MINUTES}m)"
        return 1
    fi

    return 0
}

# Check 5: Is the MCP inbox server running?
check_mcp_server() {
    local mcp_pid=$(pgrep -f "inbox_server.py" | head -1)

    if [[ -z "$mcp_pid" ]]; then
        log_warn "MCP inbox server not found (may be normal if Claude hasn't started it)"
        return 0  # Don't fail - MCP server is started by Claude
    fi

    log_info "MCP inbox server running: PID $mcp_pid"
    return 0
}

# Check 6: Memory usage
check_memory() {
    local mem_usage=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2 * 100}')

    if [[ $mem_usage -gt $MEMORY_THRESHOLD ]]; then
        log_error "Memory usage critical: ${mem_usage}% (threshold: ${MEMORY_THRESHOLD}%)"
        return 1
    fi

    log_info "Memory usage OK: ${mem_usage}%"
    return 0
}

# Check 7: Is Claude actively polling? (tmux output check)
check_claude_active() {
    # Capture recent tmux output
    local recent_output=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$SESSION_NAME" -p -S -20 2>/dev/null)

    if [[ -z "$recent_output" ]]; then
        log_warn "Could not capture tmux output"
        return 0  # Don't fail on capture failure
    fi

    # Check for signs of activity or waiting state
    if echo "$recent_output" | grep -q "wait_for_messages\|API Error\|Error:"; then
        # Check specifically for stuck state (prompt with no activity)
        if echo "$recent_output" | grep -qE "^❯\s*$" && \
           echo "$recent_output" | grep -q "API Error"; then
            log_error "Claude appears stuck at prompt after API errors"
            return 1
        fi
    fi

    return 0
}

#-------------------------------------------------------------------------------
# Recovery Actions
#-------------------------------------------------------------------------------

restart_hyperion() {
    log_warn "Initiating Hyperion restart..."

    if ! can_restart; then
        log_error "Cannot restart - rate limit exceeded. Manual intervention required."
        "$HOME/hyperion/scripts/alert.sh" "Health check: Max restart attempts exceeded. Manual intervention required."
        return 1
    fi

    record_restart

    # Gracefully stop existing session
    log_info "Stopping existing tmux session..."
    tmux -L "$TMUX_SOCKET" send-keys -t "$SESSION_NAME" "/exit" Enter 2>/dev/null
    sleep 2
    tmux -L "$TMUX_SOCKET" kill-session -t "$SESSION_NAME" 2>/dev/null
    sleep 2

    # Start new session
    log_info "Starting new Hyperion session..."
    tmux -L "$TMUX_SOCKET" new-session -d -s "$SESSION_NAME" -c "$HOME/hyperion-workspace" \
        "$HOME/hyperion/scripts/claude-wrapper.exp"

    sleep 3

    # Verify restart
    if check_tmux_session && check_claude_process; then
        log_info "Hyperion restarted successfully"

        # Touch heartbeat to give new session time to establish
        touch "$HEARTBEAT_FILE"

        return 0
    else
        log_error "Hyperion restart failed"
        return 1
    fi
}

#-------------------------------------------------------------------------------
# Main Health Check Logic
#-------------------------------------------------------------------------------

main() {
    acquire_lock

    log_info "=== Health check starting ==="

    local issues_found=0
    local critical_failure=0

    # Run all checks
    if ! check_tmux_session; then
        critical_failure=1
    fi

    if ! check_claude_process; then
        critical_failure=1
    fi

    if ! check_heartbeat; then
        issues_found=1
    fi

    if ! check_stale_messages; then
        issues_found=1
    fi

    if ! check_mcp_server; then
        issues_found=1
    fi

    if ! check_memory; then
        issues_found=1
    fi

    if ! check_claude_active; then
        critical_failure=1
    fi

    # Take action based on findings
    if [[ $critical_failure -eq 1 ]]; then
        log_error "Critical failure detected - attempting restart"
        restart_hyperion
    elif [[ $issues_found -eq 1 ]]; then
        log_warn "Non-critical issues found - monitoring"
        # Update heartbeat to show health check is active
        touch "$HOME/hyperion-workspace/logs/health-check.heartbeat"
    else
        log_info "All checks passed"
        touch "$HOME/hyperion-workspace/logs/health-check.heartbeat"
    fi

    log_info "=== Health check complete ==="
}

main "$@"
