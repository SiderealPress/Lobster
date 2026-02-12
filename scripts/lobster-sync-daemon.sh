#!/bin/bash
#===============================================================================
# lobster-sync-daemon.sh -- Daemon that continuously backs up registered repos
#
# Reads ~/.lobster/sync-config.json for the list of repositories and settings,
# then loops forever, calling lobster-sync-repo.sh for each enabled repo on
# the configured interval.
#
# Usage:
#   lobster-sync-daemon.sh              Start the daemon (foreground)
#   lobster-sync-daemon.sh --once       Run one sync cycle and exit
#   lobster-sync-daemon.sh --status     Check if daemon is running
#   lobster-sync-daemon.sh --stop       Stop a running daemon
#
# Config: ~/.lobster/sync-config.json
# Logs:   ~/.lobster/logs/sync.log
# PID:    ~/.lobster/sync.pid
#===============================================================================

set -euo pipefail

#-------------------------------------------------------------------------------
# Constants & paths
#-------------------------------------------------------------------------------

LOBSTER_DIR="${LOBSTER_SYNC_HOME:-$HOME/.lobster}"
CONFIG_FILE="${LOBSTER_SYNC_CONFIG:-$LOBSTER_DIR/sync-config.json}"
LOG_FILE="$LOBSTER_DIR/logs/sync.log"
PID_FILE="$LOBSTER_DIR/sync.pid"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="$SCRIPT_DIR/lobster-sync-repo.sh"

#-------------------------------------------------------------------------------
# Helper functions
#-------------------------------------------------------------------------------

log() {
    local timestamp
    timestamp="$(date -Iseconds)"
    printf '[%s] %s\n' "$timestamp" "$*" | tee -a "$LOG_FILE" >&2
}

die() {
    log "FATAL: $*"
    exit 1
}

ensure_dirs() {
    mkdir -p "$LOBSTER_DIR/logs"
}

#-------------------------------------------------------------------------------
# PID file management
#-------------------------------------------------------------------------------

write_pid() {
    echo $$ > "$PID_FILE"
}

remove_pid() {
    rm -f "$PID_FILE"
}

read_pid() {
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || echo ""
}

is_daemon_running() {
    local pid
    pid="$(read_pid)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

#-------------------------------------------------------------------------------
# Config parsing (uses jq for JSON)
#-------------------------------------------------------------------------------

read_config_field() {
    local field="$1"
    local default="$2"
    if [[ -f "$CONFIG_FILE" ]]; then
        local value
        value="$(jq -r "$field // empty" "$CONFIG_FILE" 2>/dev/null)"
        echo "${value:-$default}"
    else
        echo "$default"
    fi
}

get_sync_interval() {
    read_config_field '.sync_interval_seconds' '300'
}

get_sync_branch() {
    read_config_field '.sync_branch' 'lobster-sync'
}

# Returns a newline-separated list of "path|remote|enabled" triples
get_repo_entries() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        return
    fi
    # Note: jq's // operator treats false as "missing", so we cannot use
    # (.enabled // true). Instead we explicitly check for false.
    jq -r '.repos[]? | "\(.path)|\(.remote // "origin")|\(if .enabled == false then "false" else "true" end)"' "$CONFIG_FILE" 2>/dev/null
}

#-------------------------------------------------------------------------------
# Core sync cycle: iterate over all enabled repos
#-------------------------------------------------------------------------------

sync_all_repos() {
    local sync_branch
    sync_branch="$(get_sync_branch)"
    local synced=0
    local skipped=0
    local failed=0

    while IFS='|' read -r repo_path remote enabled; do
        # Skip disabled repos
        if [[ "$enabled" != "true" ]]; then
            log "Skipping disabled repo: $repo_path"
            skipped=$((skipped + 1))
            continue
        fi

        # Skip repos whose path does not exist
        if [[ ! -d "$repo_path" ]]; then
            log "WARNING: Repo path does not exist: $repo_path"
            failed=$((failed + 1))
            continue
        fi

        # Run the sync script, capturing exit code without stopping the daemon
        if "$SYNC_SCRIPT" "$repo_path" "$sync_branch" "$remote"; then
            synced=$((synced + 1))
        else
            log "WARNING: Sync failed for $repo_path (exit code $?)"
            failed=$((failed + 1))
        fi
    done <<< "$(get_repo_entries)"

    log "Cycle complete: synced=$synced skipped=$skipped failed=$failed"
}

#-------------------------------------------------------------------------------
# Signal handling for graceful shutdown
#-------------------------------------------------------------------------------

RUNNING=true

handle_signal() {
    log "Received shutdown signal, stopping..."
    RUNNING=false
}

trap handle_signal SIGTERM SIGINT SIGHUP

#-------------------------------------------------------------------------------
# Subcommands
#-------------------------------------------------------------------------------

cmd_status() {
    if is_daemon_running; then
        local pid
        pid="$(read_pid)"
        echo "lobster-sync daemon is running (PID: $pid)"
        exit 0
    else
        echo "lobster-sync daemon is not running"
        # Clean up stale PID file
        remove_pid
        exit 1
    fi
}

cmd_stop() {
    if is_daemon_running; then
        local pid
        pid="$(read_pid)"
        log "Stopping daemon (PID: $pid)"
        kill "$pid"
        # Wait for process to exit (up to 10 seconds)
        local i=0
        while kill -0 "$pid" 2>/dev/null && [[ $i -lt 10 ]]; do
            sleep 1
            i=$((i + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            log "Force killing daemon (PID: $pid)"
            kill -9 "$pid" 2>/dev/null || true
        fi
        remove_pid
        echo "Daemon stopped"
    else
        echo "Daemon is not running"
        remove_pid
    fi
    exit 0
}

cmd_once() {
    ensure_dirs
    log "Running single sync cycle"
    if [[ ! -f "$CONFIG_FILE" ]]; then
        die "Config file not found: $CONFIG_FILE"
    fi
    sync_all_repos
    exit 0
}

#-------------------------------------------------------------------------------
# Main daemon loop
#-------------------------------------------------------------------------------

cmd_daemon() {
    ensure_dirs

    # Check for existing daemon
    if is_daemon_running; then
        die "Daemon already running (PID: $(read_pid))"
    fi

    # Validate config
    if [[ ! -f "$CONFIG_FILE" ]]; then
        die "Config file not found: $CONFIG_FILE (copy from config/sync-config.example.json)"
    fi

    # Validate jq is available
    command -v jq > /dev/null 2>&1 || die "jq is required but not found"

    # Validate sync script exists
    [[ -x "$SYNC_SCRIPT" ]] || die "Sync script not found or not executable: $SYNC_SCRIPT"

    local interval
    interval="$(get_sync_interval)"

    write_pid
    # Ensure PID file is cleaned up on any exit
    trap 'remove_pid; handle_signal' EXIT

    log "Daemon started (PID: $$, interval: ${interval}s)"
    log "Config: $CONFIG_FILE"

    while $RUNNING; do
        sync_all_repos

        # Sleep in 1-second increments to remain responsive to signals
        local elapsed=0
        while $RUNNING && [[ $elapsed -lt $interval ]]; do
            sleep 1
            elapsed=$((elapsed + 1))
        done
    done

    log "Daemon stopped gracefully"
}

#-------------------------------------------------------------------------------
# Entry point: dispatch subcommand
#-------------------------------------------------------------------------------

case "${1:-}" in
    --status)  cmd_status ;;
    --stop)    cmd_stop   ;;
    --once)    cmd_once   ;;
    --help|-h)
        printf 'Usage: %s [--once|--status|--stop|--help]\n' "$(basename "$0")"
        printf '\nRun without arguments to start the daemon in the foreground.\n'
        exit 0
        ;;
    "")        cmd_daemon ;;
    *)         die "Unknown option: $1 (try --help)" ;;
esac
