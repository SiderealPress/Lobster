#!/bin/bash
# log-cleanup.sh — Prune stale log/data files to prevent unbounded directory growth.
#
# Directories cleaned:
#   ~/lobster-workspace/scheduled-jobs/logs/  — files older than 7 days
#   ~/messages/processed/                      — files older than 30 days
#   ~/messages/audio/                          — files older than 7 days
#
# Usage:
#   log-cleanup.sh           — delete stale files and print summary
#   log-cleanup.sh --dry-run — print what would be deleted, make no changes

set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"

cleanup_dir() {
    local label="$1"
    local dir="$2"
    local max_days="$3"

    if [ ! -d "$dir" ]; then
        echo "  [skip] $label: directory does not exist ($dir)"
        return
    fi

    if $DRY_RUN; then
        local count
        count=$(find "$dir" -maxdepth 1 -type f -mtime +"$max_days" | wc -l)
        echo "  [dry-run] $label: would delete $count file(s) older than ${max_days}d"
        if [ "$count" -gt 0 ]; then
            find "$dir" -maxdepth 1 -type f -mtime +"$max_days" | sort | while read -r f; do
                echo "    $f"
            done
        fi
    else
        local count
        count=$(find "$dir" -maxdepth 1 -type f -mtime +"$max_days" | wc -l)
        find "$dir" -maxdepth 1 -type f -mtime +"$max_days" -delete
        echo "  $label: deleted $count file(s) older than ${max_days}d"
    fi
}

echo "log-cleanup.sh — $(date -Iseconds)${DRY_RUN:+ [DRY RUN]}"

# scheduled-jobs/logs/ is a flat directory (no per-job subdirectories);
# -maxdepth 1 is correct and intentional here.
cleanup_dir "scheduled-jobs/logs" "$WORKSPACE/scheduled-jobs/logs" 7
cleanup_dir "messages/processed"  "$MESSAGES_DIR/processed"        30
cleanup_dir "messages/audio"      "$MESSAGES_DIR/audio"            7

echo "log-cleanup.sh done"
