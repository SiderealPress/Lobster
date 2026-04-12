#!/bin/bash
#===============================================================================
# log-cleanup.sh
#
# Prune three directories that grow without bound (issue #1240):
#
#   1. ~/lobster-workspace/scheduled-jobs/logs/  — job run logs (keep 14 days)
#   2. ~/messages/processed/                     — processed inbox messages (keep 30 days)
#   3. ~/messages/audio/                         — voice audio files (keep 7 days)
#
# Designed to be safe and idempotent:
#   - Skips deletion if the directory doesn't exist
#   - Counts deleted files and logs the result
#   - Never touches files newer than the retention window
#
# Usage (run by cron — see install.sh / upgrade.sh Migration 70):
#   0 4 * * * ~/lobster/scripts/log-cleanup.sh >> ~/lobster-workspace/logs/log-cleanup.log 2>&1
#
# The cron entry fires at 04:00 UTC daily (one hour after nightly-consolidation).
#===============================================================================

set -euo pipefail

LOBSTER_WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
LOG_DIR="$LOBSTER_WORKSPACE/logs"
LOG_FILE="$LOG_DIR/log-cleanup.log"

# Retention windows (days)
SCHED_LOGS_DAYS=14
PROCESSED_DAYS=30
AUDIO_DAYS=7

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"
}

prune_dir() {
    local label="$1"
    local dir="$2"
    local days="$3"

    if [ ! -d "$dir" ]; then
        log "SKIP $label: directory not found ($dir)"
        return 0
    fi

    # Count files before deletion
    local before
    before=$(find "$dir" -maxdepth 1 -type f 2>/dev/null | wc -l)

    # Delete files older than $days days
    find "$dir" -maxdepth 1 -type f -mtime "+${days}" -delete 2>/dev/null || true

    local after
    after=$(find "$dir" -maxdepth 1 -type f 2>/dev/null | wc -l)
    local deleted=$(( before - after ))

    log "PRUNED $label: removed $deleted file(s) older than ${days}d (${after} remaining)"
}

mkdir -p "$LOG_DIR"

log "log-cleanup.sh started"

prune_dir "scheduled-jobs/logs" "$LOBSTER_WORKSPACE/scheduled-jobs/logs" "$SCHED_LOGS_DAYS"
prune_dir "messages/processed"  "$MESSAGES_DIR/processed"               "$PROCESSED_DAYS"
prune_dir "messages/audio"      "$MESSAGES_DIR/audio"                   "$AUDIO_DAYS"

log "log-cleanup.sh complete"
