#!/bin/bash
#===============================================================================
# Lobster Inbox Watchdog - Soft Interrupt for Stale Inbox
#
# When the Claude session blocks (waiting on TaskOutput, extended thinking, etc.),
# the inbox freezes. This watchdog detects stale messages and sends Ctrl+C to
# interrupt the session, then injects a resume message.
#
# Designed to complement health-check-v3.sh:
#   - Watchdog: soft interrupt at 90s (Ctrl+C + resume message)
#   - Health check: hard restart at 180s (systemd restart)
#
# Run via cron every minute:
#   * * * * * /home/admin/lobster/scripts/inbox-watchdog.sh
#
# Internal sleep 30 gives effective ~30-second check interval.
#===============================================================================

set -o pipefail

#===============================================================================
# Configuration
#===============================================================================
TMUX_SOCKET="lobster"
TMUX_SESSION="lobster"

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"

INBOX_DIR="$MESSAGES_DIR/inbox"
STALE_THRESHOLD_SECONDS=90               # Interrupt if any message older than this
RATE_LIMIT_SECONDS=120                   # Minimum time between interrupts

STATE_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}/.state"
STATE_FILE="$STATE_DIR/watchdog-last-interrupt"
LOCK_FILE="/tmp/lobster-inbox-watchdog.lock"
LOG_FILE="$WORKSPACE_DIR/logs/watchdog.log"

# Ensure directories exist
mkdir -p "$STATE_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

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
# Locking - prevent concurrent watchdog runs
#===============================================================================
acquire_lock() {
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        exit 0
    fi
}

#===============================================================================
# Rate limiting
#===============================================================================
check_rate_limit() {
    if [[ ! -f "$STATE_FILE" ]]; then
        return 0
    fi

    local last_interrupt
    last_interrupt=$(cat "$STATE_FILE" 2>/dev/null)
    [[ -z "$last_interrupt" ]] && return 0

    local now
    now=$(date +%s)
    local elapsed=$((now - last_interrupt))

    if [[ $elapsed -lt $RATE_LIMIT_SECONDS ]]; then
        log_info "Rate limited: last interrupt ${elapsed}s ago (limit: ${RATE_LIMIT_SECONDS}s)"
        return 1
    fi

    return 0
}

record_interrupt() {
    date +%s > "$STATE_FILE"
}

#===============================================================================
# Check if Claude process is alive in tmux
#===============================================================================
claude_alive() {
    local claude_pids
    claude_pids=$(pgrep -f "claude.*--dangerously-skip-permissions" 2>/dev/null)
    [[ -z "$claude_pids" ]] && return 1

    # Verify at least one is in the lobster tmux
    local tmux_panes
    tmux_panes=$(tmux -L "$TMUX_SOCKET" list-panes -t "$TMUX_SESSION" -F '#{pane_pid}' 2>/dev/null)
    [[ -z "$tmux_panes" ]] && return 1

    for pid in $claude_pids; do
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

#===============================================================================
# Core watchdog logic
#===============================================================================
do_watchdog_check() {
    local now
    now=$(date +%s)
    local stale_count=0
    local oldest_age=0

    # Scan inbox for stale messages
    while IFS= read -r -d '' f; do
        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))
        [[ $age -gt $oldest_age ]] && oldest_age=$age

        if [[ $age -gt $STALE_THRESHOLD_SECONDS ]]; then
            stale_count=$((stale_count + 1))
        fi
    done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    # No stale messages → nothing to do
    if [[ $stale_count -eq 0 ]]; then
        return 0
    fi

    log_warn "Found $stale_count stale message(s) (oldest: ${oldest_age}s, threshold: ${STALE_THRESHOLD_SECONDS}s)"

    # Skip if Claude not alive (let health-check handle)
    if ! claude_alive; then
        log_info "Claude process not alive in tmux - skipping (health-check will handle)"
        return 0
    fi

    # Skip if a watchdog resume message already exists (already recovering)
    if find "$INBOX_DIR" -maxdepth 1 -name "*_watchdog_resume.json" -print -quit 2>/dev/null | grep -q .; then
        log_info "Watchdog resume message already in inbox - skipping"
        return 0
    fi

    # Rate limit check
    if ! check_rate_limit; then
        return 0
    fi

    # Send SIGINT (Ctrl+C) to the Claude session
    log_warn "Sending Ctrl+C to tmux session $TMUX_SESSION"
    tmux -L "$TMUX_SOCKET" send-keys -t "$TMUX_SESSION" C-c

    # Wait for Claude to process the interrupt
    sleep 2

    # Inject resume message into inbox
    local epoch_ms
    epoch_ms=$(date +%s%3N)
    local msg_id="${epoch_ms}_watchdog_resume"
    local msg_file="$INBOX_DIR/${msg_id}.json"
    local tmp_file="${msg_file}.tmp"

    cat > "$tmp_file" <<EOF
{
  "id": "${msg_id}",
  "source": "system",
  "chat_id": 0,
  "user_id": 0,
  "username": "lobster-watchdog",
  "user_name": "Lobster Watchdog",
  "text": "[WATCHDOG] Session interrupted - inbox stale >${STALE_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s, count: ${stale_count}). Return to wait_for_messages() immediately.",
  "timestamp": "$(date -Iseconds)",
  "type": "text"
}
EOF
    mv "$tmp_file" "$msg_file"

    record_interrupt
    log_warn "Interrupt sent and resume message injected: $msg_id"

    return 1
}

#===============================================================================
# Main
#===============================================================================
main() {
    acquire_lock

    # Pass 0: first check
    do_watchdog_check

    # Sleep 30 seconds for second check (gives ~30s effective interval with 1-min cron)
    sleep 30

    # Pass 1: second check
    do_watchdog_check
}

main "$@"
