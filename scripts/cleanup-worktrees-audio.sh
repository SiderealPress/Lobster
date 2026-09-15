#!/bin/bash
#===============================================================================
# cleanup-worktrees-audio.sh
#
# Periodic cleanup for two categories of stale runtime artifacts:
#
#   1. Finished git worktrees — worktrees whose branches no longer exist
#      or have been merged. `git worktree prune` removes entries for worktrees
#      whose directories are gone; this script additionally removes any
#      lingering worktree directories under LOBSTER_PROJECTS_DIR.
#
#   2. Old audio files — voice message audio in ~/messages/audio/ older than
#      AUDIO_RETENTION_DAYS. These files are transcribed and stored as text;
#      the originals are only retained for debugging and can be safely deleted.
#
#   3. Stale synthetic test fixtures in ~/messages/failed/ — the unrecognized-
#      source quarantine guard in src/mcp/inbox_server.py moves any inbox
#      message with source="test" to failed/ as `test_<epoch>.json` (issue
#      #2209). These accumulate indefinitely and drown out genuine quarantined
#      messages in the same directory. Only files matching the exact
#      `test_<digits>.json` pattern are removed, and only once older than
#      FAILED_TEST_FIXTURE_RETENTION_DAYS — every other file in failed/
#      (including real quarantined messages) is left untouched regardless of
#      age or name.
#
# Usage:
#   ~/lobster/scripts/cleanup-worktrees-audio.sh
#
# Typically registered as a cron job (daily at 04:00):
#   0 4 * * * ~/lobster/scripts/cleanup-worktrees-audio.sh >> ~/lobster-workspace/logs/cleanup.log 2>&1
#===============================================================================

set -euo pipefail

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
PROJECTS_DIR="${LOBSTER_PROJECTS:-$WORKSPACE_DIR/projects}"
# AUDIO_DIR can be overridden directly (useful for tests); falls back to LOBSTER_MESSAGES/audio.
AUDIO_DIR="${CLEANUP_AUDIO_DIR:-${LOBSTER_MESSAGES:-$HOME/messages}/audio}"
AUDIO_RETENTION_DAYS="${CLEANUP_AUDIO_RETENTION_DAYS:-7}"
# FAILED_DIR can be overridden directly (useful for tests); falls back to LOBSTER_MESSAGES/failed.
FAILED_DIR="${CLEANUP_FAILED_DIR:-${LOBSTER_MESSAGES:-$HOME/messages}/failed}"
FAILED_TEST_FIXTURE_RETENTION_DAYS="${CLEANUP_FAILED_TEST_FIXTURE_RETENTION_DAYS:-1}"

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

log()  { echo "[$(timestamp)] $*"; }
info() { echo "[$(timestamp)] INFO  $*"; }
warn() { echo "[$(timestamp)] WARN  $*" >&2; }

#-------------------------------------------------------------------------------
# 1. Prune git worktrees
#    - Prune stale administrative entries in both git repos
#    - Remove empty worktree directories left behind in projects/
#-------------------------------------------------------------------------------

prune_worktrees() {
    info "Pruning worktrees in $LOBSTER_DIR"

    if [ -d "$LOBSTER_DIR/.git" ] || [ -f "$LOBSTER_DIR/.git" ]; then
        git -C "$LOBSTER_DIR" worktree prune --verbose 2>&1 | while IFS= read -r line; do
            log "  [git worktree prune] $line"
        done || warn "git worktree prune failed for $LOBSTER_DIR"
    else
        warn "$LOBSTER_DIR is not a git repo — skipping worktree prune"
    fi

    # Remove empty directories in PROJECTS_DIR that are no longer registered
    # worktrees. We only remove directories that:
    #   a) Are direct children of PROJECTS_DIR
    #   b) No longer appear in `git worktree list` output
    if [ -d "$PROJECTS_DIR" ] && [ -d "$LOBSTER_DIR/.git" -o -f "$LOBSTER_DIR/.git" ] 2>/dev/null; then
        # Collect worktrees that git still knows about (absolute paths)
        local registered_worktrees
        registered_worktrees=$(git -C "$LOBSTER_DIR" worktree list --porcelain 2>/dev/null \
            | grep '^worktree ' | awk '{print $2}')

        local removed_dirs=0
        while IFS= read -r -d '' candidate; do
            local dir_path
            dir_path=$(realpath "$candidate" 2>/dev/null || echo "$candidate")
            if ! echo "$registered_worktrees" | grep -qxF "$dir_path"; then
                if [ -d "$dir_path" ] && [ -z "$(ls -A "$dir_path" 2>/dev/null)" ]; then
                    rmdir "$dir_path"
                    info "Removed empty unregistered worktree directory: $dir_path"
                    removed_dirs=$(( removed_dirs + 1 ))
                fi
            fi
        done < <(find "$PROJECTS_DIR" -maxdepth 1 -mindepth 1 -type d -print0 2>/dev/null)

        if [ "$removed_dirs" -eq 0 ]; then
            info "No empty unregistered worktree directories to remove"
        else
            info "Removed $removed_dirs empty worktree director(ies)"
        fi
    fi
}

#-------------------------------------------------------------------------------
# 2. Delete old audio files
#    Audio extensions produced by Telegram voice messages and video notes.
#    Only files older than AUDIO_RETENTION_DAYS are removed.
#-------------------------------------------------------------------------------

prune_audio() {
    if [ ! -d "$AUDIO_DIR" ]; then
        info "Audio directory $AUDIO_DIR does not exist — skipping"
        return 0
    fi

    info "Removing audio files older than ${AUDIO_RETENTION_DAYS} day(s) from $AUDIO_DIR"

    local deleted=0
    while IFS= read -r -d '' filepath; do
        rm -f "$filepath"
        info "Deleted old audio: $filepath"
        deleted=$(( deleted + 1 ))
    done < <(find "$AUDIO_DIR" \
        \( -name "*.ogg" -o -name "*.mp3" -o -name "*.m4a" -o -name "*.wav" -o -name "*.oga" \) \
        -mtime "+${AUDIO_RETENTION_DAYS}" \
        -print0 2>/dev/null)

    if [ "$deleted" -eq 0 ]; then
        info "No audio files older than ${AUDIO_RETENTION_DAYS} days found"
    else
        info "Deleted $deleted audio file(s)"
    fi
}

#-------------------------------------------------------------------------------
# 3. Prune stale synthetic test fixtures from ~/messages/failed/ (issue #2209)
#    Only files matching the exact `test_<digits>.json` name are candidates;
#    every other filename in failed/ (including real quarantined messages) is
#    never touched by this step, regardless of age.
#-------------------------------------------------------------------------------

prune_failed_test_fixtures() {
    if [ ! -d "$FAILED_DIR" ]; then
        info "Failed-messages directory $FAILED_DIR does not exist — skipping"
        return 0
    fi

    info "Removing test_<digits>.json fixtures older than ${FAILED_TEST_FIXTURE_RETENTION_DAYS} day(s) from $FAILED_DIR"

    local deleted=0
    while IFS= read -r -d '' filepath; do
        rm -f "$filepath"
        info "Deleted stale test fixture: $filepath"
        deleted=$(( deleted + 1 ))
    done < <(find "$FAILED_DIR" \
        -maxdepth 1 \
        -type f \
        -regextype posix-extended -regex '.*/test_[0-9]+\.json' \
        -mtime "+${FAILED_TEST_FIXTURE_RETENTION_DAYS}" \
        -print0 2>/dev/null)

    if [ "$deleted" -eq 0 ]; then
        info "No stale test_<digits>.json fixtures found in $FAILED_DIR"
    else
        info "Deleted $deleted stale test fixture(s) from $FAILED_DIR"
    fi
}

#-------------------------------------------------------------------------------
# Main
#-------------------------------------------------------------------------------

prune_pr_worktrees() {
    local script="$LOBSTER_DIR/scripts/prune-pr-worktrees.py"
    if [ ! -f "$script" ]; then
        info "prune-pr-worktrees.py not found — skipping"
        return 0
    fi
    info "Pruning stale PR worktrees (merged/closed, ≥7 days old)"
    uv run "$script" --age-days 7 2>&1 | while IFS= read -r line; do
        log "  [prune-pr-worktrees] $line"
    done || warn "prune-pr-worktrees.py exited non-zero"
}

main() {
    log "=== cleanup-worktrees-audio.sh starting ==="
    prune_worktrees
    prune_pr_worktrees
    prune_audio
    prune_failed_test_fixtures
    log "=== cleanup-worktrees-audio.sh complete ==="
}

main "$@"
