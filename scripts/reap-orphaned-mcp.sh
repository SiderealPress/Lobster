#!/bin/bash
#===============================================================================
# reap-orphaned-mcp.sh — Kill orphaned MCP server and browser processes
#
# MCP server node processes (google-workspace-mcp, obsidian-mcp, etc.) are
# spawned as children of subagent Claude processes. When a subagent exits or
# gets stuck, these node servers become orphaned — their parent Claude process
# is gone but the node processes continue running, leaking RAM and file
# descriptors.
#
# Additionally, camoufox-browser (the anti-detection browser skill) spawns
# Chromium/camoufox subprocesses. When Claude sessions end abnormally, these
# browser processes can become orphaned and accumulate, consuming significant
# RAM and CPU. The camofox-browser Node.js server is managed by a systemd
# user service, so only Chromium subprocesses it spawned as orphaned children
# are targeted — the server process itself is skipped if it is a tmux ancestor.
#
# This script runs periodically from cron to reap all such orphans.
#
# Safety contract:
#   - Only kills processes matching the known patterns below (MCP servers +
#     Chromium/camoufox subprocesses)
#   - Skips any process that is a descendant of the active lobster tmux session
#     (meaning it is still being used by a live session)
#   - SIGTERM first, SIGKILL only after a 10-second grace period
#   - SIGKILL is only sent to PIDs that received SIGTERM (avoids PID-reuse kills)
#   - Logs all actions to WORKSPACE/logs/reap-orphaned-mcp.log
#
# Usage:
#   scripts/reap-orphaned-mcp.sh
#
# Cron (typically every hour):
#   0 * * * * /home/lobster/lobster/scripts/reap-orphaned-mcp.sh
#
# Issues: #1108 — Scheduler leaves orphaned processes
#          #1262 — Camoufox orphaned Chrome subprocesses
#===============================================================================

set -uo pipefail

LOBSTER_WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOG_DIR="$LOBSTER_WORKSPACE/logs"
LOG_FILE="$LOG_DIR/reap-orphaned-mcp.log"

mkdir -p "$LOG_DIR"

log() {
    local msg="[$(date -Iseconds)] $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# MCP server process patterns to match
# Each pattern is used with pgrep -f to find running processes.
#
# MCP server node processes (orphaned when subagent Claude sessions exit):
#   google-workspace-mcp, obsidian-mcp, @modelcontextprotocol/server, mcp-server-*
#
# Camoufox browser Chromium subprocesses (orphaned when sessions end abnormally).
# The camoufox-js binary is typically named "camoufox" or ".camoufox-real";
# Chromium subprocesses are launched as --type=renderer, --type=gpu-process, etc.
# We match on the camoufox binary path (inside node_modules) to avoid
# killing any system-installed Chromium that is unrelated to Lobster.
# ---------------------------------------------------------------------------
MCP_PATTERNS=(
    "google-workspace-mcp"
    "obsidian-mcp"
    "@modelcontextprotocol/server"
    "mcp-server-"
    # camoufox/camoufox-js spawns Chromium subprocesses; match by the camoufox
    # binary path so we only target browser processes owned by the lobster install.
    "node_modules/.bin/camoufox"
    "node_modules/camoufox-js"
    ".camoufox-real"
    "camoufox-browser/server"
)

# ---------------------------------------------------------------------------
# Collect current tmux pane PIDs for the lobster session.
# Any process whose ancestor chain reaches one of these PIDs is "ours" —
# still serving an active Claude session — and must be skipped.
# ---------------------------------------------------------------------------
get_tmux_pane_pids() {
    tmux -L lobster list-panes -a -F '#{pane_pid}' 2>/dev/null || true
}

# Return "true" if $1 is a descendant of any PID in $2 (newline-separated list).
is_descendant_of_tmux() {
    local pid="$1"
    local pane_pids="$2"

    if [[ -z "$pane_pids" ]]; then
        return 1  # No tmux session → nothing is "ours"
    fi

    local check_pid="$pid"
    for _hop in 1 2 3 4 5 6 7 8 9 10; do
        local ppid
        ppid=$(ps -o ppid= -p "$check_pid" 2>/dev/null | tr -d ' ')
        if [[ -z "$ppid" || "$ppid" == "1" ]]; then
            return 1  # Reached init — orphan
        fi
        if echo "$pane_pids" | grep -qw "$ppid"; then
            return 0  # Ancestor is a tmux pane — this is ours
        fi
        check_pid="$ppid"
    done

    return 1  # Walked 10 levels without finding a tmux ancestor — treat as orphan
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log "=== reap-orphaned-mcp.sh starting ==="

    local tmux_panes
    tmux_panes=$(get_tmux_pane_pids)

    if [[ -n "$tmux_panes" ]]; then
        log "Active tmux pane PIDs: $(echo "$tmux_panes" | tr '\n' ' ')"
    else
        log "No active lobster tmux session — all matching processes are orphans"
    fi

    # Collect all candidate PIDs across all patterns, deduplicated
    local all_pids=""
    for pattern in "${MCP_PATTERNS[@]}"; do
        local found
        found=$(pgrep -f "$pattern" 2>/dev/null || true)
        if [[ -n "$found" ]]; then
            all_pids="$all_pids $found"
        fi
    done

    # Deduplicate and filter to numeric PIDs only
    all_pids=$(echo "$all_pids" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true)

    if [[ -z "$all_pids" ]]; then
        log "No orphaned MCP/browser processes found — nothing to reap"
        log "=== done ==="
        return 0
    fi

    log "Candidate PID(s): $(echo "$all_pids" | tr '\n' ' ')"

    local to_kill=()
    local skipped=0

    for pid in $all_pids; do
        # Skip if process already gone
        if ! kill -0 "$pid" 2>/dev/null; then
            continue
        fi

        # Identify the process command for logging
        local cmd
        cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "unknown")

        if is_descendant_of_tmux "$pid" "$tmux_panes"; then
            log "SKIP PID $pid ($cmd) — descendant of active lobster tmux session"
            skipped=$((skipped + 1))
        else
            log "ORPHAN PID $pid ($cmd) — no live tmux ancestor, queued for SIGTERM"
            to_kill+=("$pid")
        fi
    done

    if [[ ${#to_kill[@]} -eq 0 ]]; then
        log "No orphaned MCP processes to kill (skipped $skipped in-session)"
        log "=== done ==="
        return 0
    fi

    # SIGTERM pass
    local sigterm_sent=()
    for pid in "${to_kill[@]}"; do
        if kill -TERM "$pid" 2>/dev/null; then
            log "SIGTERM sent to PID $pid"
            sigterm_sent+=("$pid")
        else
            log "SIGTERM failed for PID $pid (already gone?)"
        fi
    done

    # Grace period
    sleep 10

    # SIGKILL pass — only PIDs that received SIGTERM (avoids PID-reuse kills)
    local killed=0
    for pid in "${sigterm_sent[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            log "SIGKILL sent to PID $pid (still alive after grace period)"
            kill -KILL "$pid" 2>/dev/null || true
            killed=$((killed + 1))
        else
            log "PID $pid exited cleanly after SIGTERM"
            killed=$((killed + 1))
        fi
    done

    log "Reaped $killed orphaned MCP process(es), skipped $skipped in-session"
    log "=== done ==="
}

main "$@"
