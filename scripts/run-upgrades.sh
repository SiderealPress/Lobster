#!/usr/bin/env bash
#===============================================================================
# run-upgrades.sh — Safe system package upgrade wrapper for Lobster
#
# Problem this solves:
#   Running apt-get upgrade directly (e.g. from the health check) causes
#   needrestart to fire SIGTERM at all lobster Python services after every
#   dpkg run. This bypasses restart-mcp.sh, which means the MCP session is
#   killed with no inbox warning — leaving the dispatcher blind until the
#   reconciler detects the dead session (typically 30+ seconds of downtime).
#
# How this script fixes it:
#   1. Calls restart-mcp.sh FIRST (writes inbox warning, waits 2s, restarts
#      lobster-mcp-local cleanly). The MCP is already restarted and the
#      dispatcher has been warned before any dpkg hook can fire.
#   2. Runs the OS package upgrade. needrestart may still fire, but the MCP
#      service is already restarted — there is no live session to invalidate.
#
# Schedule:
#   Installed by install.sh and Migration 87 (upgrade.sh) as a weekly cron
#   job running Sundays at 02:00 (LOBSTER-WEEKLY-UPGRADE marker).
#   Adjust the schedule in crontab if you want a different cadence.
#
# Usage:
#   ~/lobster/scripts/run-upgrades.sh [--dry-run]
#
# With --dry-run: prints what would happen, performs no restarts or upgrades.
#===============================================================================

set -euo pipefail

INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOG_FILE="$WORKSPACE_DIR/logs/run-upgrades.log"
TIMESTAMP=$(date -Iseconds)
DRY_RUN=false

mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$TIMESTAMP] $*" | tee -a "$LOG_FILE"; }

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    log "INFO: dry-run mode — no restarts or upgrades will be performed"
fi

# Developer mode: suppress all system activity so the developer isn't
# disturbed while testing.
_LOBSTER_CONFIG="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"
if [ -f "$_LOBSTER_CONFIG" ]; then
    _DEV_MODE=$(grep -m1 '^LOBSTER_DEV_MODE=' "$_LOBSTER_CONFIG" 2>/dev/null | cut -d= -f2 || true)
    if [ "$_DEV_MODE" = "true" ] || [ "$_DEV_MODE" = "1" ]; then
        log "INFO: LOBSTER_DEV_MODE is set — skipping upgrade run"
        exit 0
    fi
fi
unset _LOBSTER_CONFIG _DEV_MODE

log "=== run-upgrades.sh starting ==="

#-------------------------------------------------------------------------------
# Step 1: Restart MCP cleanly BEFORE any package upgrade.
#
# restart-mcp.sh writes an inbox warning, waits 2 seconds, then restarts
# lobster-mcp-local. This ensures the dispatcher sees the warning and the
# MCP session is already invalidated on our terms — before needrestart gets
# a chance to send an unexpected SIGTERM.
#-------------------------------------------------------------------------------
RESTART_SCRIPT="$INSTALL_DIR/scripts/restart-mcp.sh"

if [ ! -x "$RESTART_SCRIPT" ]; then
    log "ERROR: restart-mcp.sh not found or not executable at $RESTART_SCRIPT"
    exit 1
fi

if $DRY_RUN; then
    log "INFO: [dry-run] Would call $RESTART_SCRIPT --no-wait"
else
    log "INFO: Restarting MCP cleanly before upgrade (writes inbox warning)..."
    "$RESTART_SCRIPT" --no-wait
    log "INFO: MCP restarted — proceeding with package upgrade"
fi

#-------------------------------------------------------------------------------
# Step 2: Run the OS package upgrade.
#
# By the time we reach this point, lobster-mcp-local has already been
# restarted. If needrestart fires again after dpkg runs, the service will
# restart a second time — but there is no in-flight MCP session to lose.
#-------------------------------------------------------------------------------
sudo_prefix=""
if [ "$(id -u)" -ne 0 ]; then
    sudo_prefix="sudo "
fi

run_upgrade() {
    if command -v apt-get &>/dev/null; then
        log "INFO: Updating package lists (apt-get update)..."
        ${sudo_prefix}apt-get update -q >>"$LOG_FILE" 2>&1 || {
            log "WARN: apt-get update returned non-zero — proceeding with upgrade for trusted repos"
        }
        log "INFO: Running apt-get upgrade..."
        ${sudo_prefix}apt-get upgrade -y -q >>"$LOG_FILE" 2>&1 && \
            log "OK: system packages upgraded (apt-get)" || \
            { log "ERROR: apt-get upgrade failed"; return 1; }
    elif command -v dnf &>/dev/null; then
        log "INFO: Running dnf upgrade..."
        ${sudo_prefix}dnf upgrade -y -q >>"$LOG_FILE" 2>&1 && \
            log "OK: system packages upgraded (dnf)" || \
            { log "ERROR: dnf upgrade failed"; return 1; }
    elif command -v yum &>/dev/null; then
        log "INFO: Running yum upgrade..."
        ${sudo_prefix}yum upgrade -y -q >>"$LOG_FILE" 2>&1 && \
            log "OK: system packages upgraded (yum)" || \
            { log "ERROR: yum upgrade failed"; return 1; }
    elif command -v pacman &>/dev/null; then
        log "INFO: Running pacman -Syu..."
        ${sudo_prefix}pacman -Syu --noconfirm >>"$LOG_FILE" 2>&1 && \
            log "OK: system packages upgraded (pacman)" || \
            { log "ERROR: pacman upgrade failed"; return 1; }
    elif command -v zypper &>/dev/null; then
        log "INFO: Running zypper update..."
        ${sudo_prefix}zypper update -y >>"$LOG_FILE" 2>&1 && \
            log "OK: system packages upgraded (zypper)" || \
            { log "ERROR: zypper update failed"; return 1; }
    elif command -v apk &>/dev/null; then
        log "INFO: Running apk upgrade..."
        ${sudo_prefix}apk update >>"$LOG_FILE" 2>&1 && \
        ${sudo_prefix}apk upgrade >>"$LOG_FILE" 2>&1 && \
            log "OK: system packages upgraded (apk)" || \
            { log "ERROR: apk upgrade failed"; return 1; }
    else
        log "WARN: No supported package manager found — skipping upgrade"
    fi
}

if $DRY_RUN; then
    log "INFO: [dry-run] Would run OS package upgrade"
else
    run_upgrade
fi

log "=== run-upgrades.sh complete ==="
