#!/bin/bash
#===============================================================================
# cleanup-stale-task-outputs.sh
#
# Delete stale non-symlink *.output files from Claude Code task directories.
#
# Claude Code writes two kinds of files to tasks/ directories:
#   1. Symlinks (lrwxrwxrwx) — real subagent output files, never deleted here
#   2. Regular files (-rw-r--r--) — persisted bash tool stdout, safe to delete
#
# This script removes regular (non-symlink) *.output files older than 30 minutes.
# It is safe to run at any time: symlinks and recent files are always preserved.
#
# Usage:
#   /home/admin/lobster/scripts/cleanup-stale-task-outputs.sh
#
# Typically called from Lobster startup or via cron (every 30 minutes).
#===============================================================================

set -euo pipefail

STALE_MINUTES=30
CLAUDE_UID=$(id -u)
CLAUDE_TMP_BASE="/tmp/claude-${CLAUDE_UID}"

# Bail silently if the tmp directory doesn't exist
if [ ! -d "$CLAUDE_TMP_BASE" ]; then
    exit 0
fi

deleted=0

# Find all non-symlink *.output files in tasks/ subdirs older than STALE_MINUTES
while IFS= read -r -d '' filepath; do
    rm -f "$filepath"
    echo "Deleted stale task output: $filepath" >&2
    deleted=$(( deleted + 1 ))
done < <(find "$CLAUDE_TMP_BASE" \
    -path "*/tasks/*.output" \
    -not -type l \
    -mmin "+${STALE_MINUTES}" \
    -print0 2>/dev/null)

exit 0
