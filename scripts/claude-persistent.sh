#!/bin/bash
#===============================================================================
# Lobster Persistent Claude Session
#
# Replaces the old claude-wrapper.sh (polling --print mode) with a persistent
# Claude session that stays alive, using wait_for_messages() to block between
# message batches.
#
# Lifecycle state machine:
#   STOPPED    -> no Claude process
#   STARTING   -> this script is launching Claude
#   WAITING    -> Claude is blocked on wait_for_messages() (primary state)
#   PROCESSING -> Claude is handling a message batch
#   DELEGATING -> Claude spawned a subagent for substantial work
#   HIBERNATING -> Claude exited cleanly, wrote state to handoff doc
#
# Key design changes from claude-wrapper.sh:
#   - Claude runs persistently (not one-shot --print per batch)
#   - Uses --resume to maintain context across restarts
#   - State file tracks lifecycle phase for health check coordination
#   - Clean hibernation support: Claude can exit and write state
#   - Outer loop only restarts on abnormal exit, not routine lifecycle
#
# The systemd service should run this script directly.
#===============================================================================

set -uo pipefail
# Note: not using set -e because we handle exit codes explicitly

WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
STATE_FILE="$MESSAGES_DIR/config/lobster-state.json"
LOG_DIR="$WORKSPACE_DIR/logs"
LOG_FILE="$LOG_DIR/claude-persistent.log"

# Ensure directories exist
mkdir -p "$MESSAGES_DIR/config" "$LOG_DIR"

# Ensure Claude is in PATH
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

#===============================================================================
# Model Tiering Configuration
#
# The dispatcher runs on Sonnet for cost efficiency (~40% cheaper than Opus).
# Subagents that don't specify an explicit model in their .md frontmatter
# will inherit Sonnet via CLAUDE_CODE_SUBAGENT_MODEL.
# Agents needing Opus (functional-engineer, gsd-debugger) override explicitly.
#
# To revert dispatcher to Opus: remove --model sonnet from launch_claude()
# To revert subagents to Opus: unset CLAUDE_CODE_SUBAGENT_MODEL
#===============================================================================
export CLAUDE_CODE_SUBAGENT_MODEL=sonnet

# Session isolation guard: mark this as the designated main Lobster session.
# The MCP inbox_server.py checks for this before allowing inbox monitoring and
# outbox writes (check_inbox, wait_for_messages, send_reply, mark_processed,
# etc.). Any Claude session launched without this script will be blocked from
# those tools, preventing dual-processing when an SSH user also runs Claude.
export LOBSTER_MAIN_SESSION=1

# Trigger context compaction at 80% capacity instead of default 95%.
# Keeps peak context size lower, reducing token costs per turn.
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80

#===============================================================================
# Logging
#===============================================================================
log() {
    local msg="[$(date -Iseconds)] $1"
    echo "$msg" >> "$LOG_FILE"
    echo "$msg"
}

#===============================================================================
# State Management
#===============================================================================
write_state() {
    local mode="$1"
    local detail="${2:-}"
    local now
    now=$(date -Iseconds)
    cat > "$STATE_FILE" << EOF
{
  "mode": "$mode",
  "detail": "$detail",
  "updated_at": "$now",
  "pid": $$
}
EOF
}

read_state_mode() {
    if [[ -f "$STATE_FILE" ]]; then
        python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('mode', 'unknown'))
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}

#===============================================================================
# Preflight Checks
#===============================================================================
preflight() {
    # Verify claude is available
    if ! command -v claude &>/dev/null; then
        log "ERROR: claude not found in PATH"
        exit 1
    fi

    # Verify Claude Code is authenticated
    if ! claude auth status &>/dev/null 2>&1; then
        log "ERROR: Claude Code is not authenticated. Run: claude auth login"
        exit 1
    fi

    # Verify CLAUDE.md exists
    if [[ ! -f "$WORKSPACE_DIR/CLAUDE.md" ]]; then
        log "WARNING: $WORKSPACE_DIR/CLAUDE.md not found"
    fi

    log "Preflight checks passed"
}

#===============================================================================
# Find the most recent session to resume
#===============================================================================
find_session_to_resume() {
    # Look for the most recent session in the workspace
    # claude -r picks up the last session, but we can also check state
    local last_session=""
    if [[ -f "$STATE_FILE" ]]; then
        last_session=$(python3 -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null)
    fi
    echo "$last_session"
}

#===============================================================================
# Launch Claude in persistent mode
#===============================================================================
launch_claude() {
    local attempt="$1"

    write_state "starting" "attempt=$attempt"
    log "STARTING: Launching Claude (attempt $attempt)"

    cd "$WORKSPACE_DIR"

    # -------------------------------------------------------------------------
    # Clean leaked Claude Code env vars before launching.
    #
    # Claude Code sets CLAUDECODE=1 and CLAUDE_CODE_ENTRYPOINT in its own
    # process environment at startup. These can leak into tmux's global
    # environment (via shell snapshot creation or subprocesses). On the next
    # restart cycle, the new claude binary sees CLAUDECODE=1 and refuses to
    # launch ("cannot be launched inside another Claude Code session"),
    # causing an unrecoverable crash loop.
    #
    # Fix: strip these from both the shell environment AND tmux's global
    # environment before every launch attempt. LOBSTER_MAIN_SESSION (our own
    # session isolation guard) is unaffected — it lives in the MCP server and
    # checks a different variable.
    # -------------------------------------------------------------------------
    unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true
    if command -v tmux &>/dev/null; then
        tmux -L lobster set-environment -g -u CLAUDECODE 2>/dev/null || true
        tmux -L lobster set-environment -g -u CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true
    fi

    # Build the initial prompt for Claude
    local init_prompt="Read CLAUDE.md and begin your main loop. Call wait_for_messages(hibernate_on_timeout=true) to start listening for Telegram messages. Process each message as it arrives, then return to wait_for_messages(). Never exit unless hibernating."

    # Always start fresh. Never use --continue.
    #
    # Why: --continue resumes the previous session's context. If that session
    # was mid-task (e.g. deep in a subagent chain), Claude resumes the old
    # work instead of re-entering the message loop. The dispatcher is stateless
    # by design — it reads CLAUDE.md, enters the loop, and processes messages.
    # Any persistent state lives in canonical memory files, not conversation history.
    local claude_exit_code=0
    log "Starting fresh session (attempt $attempt)..."
    claude --dangerously-skip-permissions \
        --model sonnet \
        --max-turns 150 \
        -p "$init_prompt" \
        2>&1 | tee -a "$LOG_DIR/claude-session.log" || claude_exit_code=$?

    return $claude_exit_code
}

#===============================================================================
# Handle Claude exit
#===============================================================================
handle_exit() {
    local exit_code="$1"
    local current_mode
    current_mode=$(read_state_mode)

    if [[ "$exit_code" -eq 0 ]]; then
        # Clean exit - check if it was intentional hibernation
        if [[ "$current_mode" == "hibernate" ]]; then
            log "HIBERNATING: Claude exited cleanly (hibernation). Will wait for wake signal."
            return 0
        else
            # Claude exited cleanly but not in hibernate mode
            # This can happen when --max-turns is exhausted
            log "Claude exited cleanly (code 0) but not in hibernate mode. Will restart."
            write_state "restarting" "clean exit, max-turns likely exhausted"
            return 1
        fi
    else
        log "Claude exited with code $exit_code. Will restart after backoff."
        write_state "restarting" "exit_code=$exit_code"
        return 1
    fi
}

#===============================================================================
# Wait for wake signal (when hibernating)
#===============================================================================
wait_for_wake() {
    log "Waiting for wake signal (new inbox messages)..."
    local inbox_dir="$MESSAGES_DIR/inbox"

    while true; do
        local msg_count
        msg_count=$(find "$inbox_dir" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)

        if [[ "$msg_count" -gt 0 ]]; then
            log "Wake signal: $msg_count message(s) in inbox"
            write_state "waking" "messages=$msg_count"
            return 0
        fi

        sleep 10
    done
}

#===============================================================================
# Main Loop
#===============================================================================
main() {
    log "================================================================"
    log "Lobster Persistent Claude Session starting"
    log "Workspace: $WORKSPACE_DIR"
    log "State file: $STATE_FILE"
    log "================================================================"

    preflight

    local attempt=0
    local max_rapid_restarts=5
    local rapid_restart_window=300  # 5 minutes
    local rapid_restart_count=0
    local last_restart_time=0

    while true; do
        attempt=$((attempt + 1))
        local now
        now=$(date +%s)

        # Rapid restart detection: if we've restarted too many times too fast,
        # back off significantly
        local elapsed=$((now - last_restart_time))
        if [[ $elapsed -lt $rapid_restart_window ]]; then
            rapid_restart_count=$((rapid_restart_count + 1))
        else
            rapid_restart_count=1
        fi
        last_restart_time=$now

        if [[ $rapid_restart_count -gt $max_rapid_restarts ]]; then
            local backoff=120
            log "BACKOFF: $rapid_restart_count rapid restarts in ${rapid_restart_window}s window. Sleeping ${backoff}s..."
            write_state "backoff" "rapid_restarts=$rapid_restart_count"
            sleep $backoff
            rapid_restart_count=0
        fi

        # Launch Claude
        write_state "active" "starting claude"
        launch_claude "$attempt"
        local exit_code=$?

        # Handle the exit
        if handle_exit "$exit_code"; then
            # Clean hibernation - wait for new messages
            wait_for_wake
            attempt=0  # Reset attempt counter after clean cycle
        else
            # Abnormal exit - brief pause before restart
            local restart_delay=5
            if [[ $rapid_restart_count -gt 2 ]]; then
                restart_delay=$((rapid_restart_count * 10))
            fi
            log "Restarting in ${restart_delay}s..."
            sleep $restart_delay
        fi
    done
}

# Trap signals for clean shutdown
trap 'log "Received SIGTERM, shutting down..."; write_state "stopped" "sigterm"; exit 0' SIGTERM
trap 'log "Received SIGINT, shutting down..."; write_state "stopped" "sigint"; exit 0' SIGINT

main "$@"
