#!/bin/bash
#===============================================================================
# lobster-sync-repo.sh -- Zero-disruption git working tree backup
#
# Snapshots a repo's full working tree (including uncommitted and untracked
# files) to a sync branch on the remote WITHOUT touching the working directory,
# staged changes, or current branch.
#
# Uses git plumbing commands (write-tree, commit-tree, update-ref) with a
# temporary index file to achieve complete non-invasiveness.
#
# Usage: lobster-sync-repo.sh <repo-path> [sync-branch] [remote]
#
# Arguments:
#   repo-path    Path to the git repository to sync
#   sync-branch  Branch name for sync commits (default: lobster-sync)
#   remote       Remote to push to (default: origin)
#
# Environment:
#   LOBSTER_SYNC_DRY_RUN=1  Skip the push step (useful for testing)
#   LOBSTER_SYNC_QUIET=1    Suppress informational output
#
# Exit codes:
#   0  Success (synced or skipped because unchanged)
#   1  Error (not a git repo, git command failed, etc.)
#   2  Usage error (missing arguments)
#===============================================================================

set -euo pipefail

#-------------------------------------------------------------------------------
# Arguments & defaults
#-------------------------------------------------------------------------------

REPO_PATH="${1:-}"
SYNC_BRANCH="${2:-lobster-sync}"
REMOTE="${3:-origin}"
DRY_RUN="${LOBSTER_SYNC_DRY_RUN:-0}"
QUIET="${LOBSTER_SYNC_QUIET:-0}"

#-------------------------------------------------------------------------------
# Helper functions (pure output, no side effects)
#-------------------------------------------------------------------------------

log() {
    [[ "$QUIET" == "1" ]] && return
    printf '[lobster-sync] %s\n' "$*" >&2
}

die() {
    printf '[lobster-sync] ERROR: %s\n' "$*" >&2
    exit 1
}

#-------------------------------------------------------------------------------
# Validation
#-------------------------------------------------------------------------------

[[ -z "$REPO_PATH" ]] && {
    printf 'Usage: %s <repo-path> [sync-branch] [remote]\n' "$(basename "$0")" >&2
    exit 2
}

# Resolve to absolute path
REPO_PATH="$(cd "$REPO_PATH" 2>/dev/null && pwd)" || die "Cannot access directory: $1"

# Verify it is a git repository
git -C "$REPO_PATH" rev-parse --git-dir > /dev/null 2>&1 || \
    die "Not a git repository: $REPO_PATH"

#-------------------------------------------------------------------------------
# Temp index setup with guaranteed cleanup
#-------------------------------------------------------------------------------

# mktemp reserves a unique filename. We then remove the zero-byte file
# because git expects the index file to either not exist (so it creates one
# with a valid header) or to have a valid git index header. A zero-byte
# file causes "index file smaller than expected".
TEMP_INDEX="$(mktemp "${TMPDIR:-/tmp}/lobster-sync-index.XXXXXX")"
rm -f "$TEMP_INDEX"

cleanup() {
    rm -f "$TEMP_INDEX"
}
trap cleanup EXIT

#-------------------------------------------------------------------------------
# Build tree from working directory using temp index
#
# Key invariant: GIT_INDEX_FILE points to our temp file, so the real
# .git/index is never read or written. The working directory is only
# read (via git add -A on the temp index), never modified.
#-------------------------------------------------------------------------------

log "Syncing $REPO_PATH to $SYNC_BRANCH"

# Determine the git directory (handles worktrees and submodules)
GIT_DIR="$(git -C "$REPO_PATH" rev-parse --git-dir)"

# If there is a .lobster-sync-exclude file, configure git to use it as an
# additional excludes file for this operation only. This is merged with
# .gitignore automatically by git.
EXCLUDE_ARGS=()
EXCLUDE_FILE="$REPO_PATH/.lobster-sync-exclude"
if [[ -f "$EXCLUDE_FILE" ]]; then
    EXCLUDE_ARGS=(-c "core.excludesFile=$EXCLUDE_FILE")
    log "Using exclusions from .lobster-sync-exclude"
fi

# Stage ALL files (including untracked) into the temp index.
# This respects .gitignore and .lobster-sync-exclude but captures
# everything else -- uncommitted changes, new files, etc.
# Note: ${EXCLUDE_ARGS[@]+"${EXCLUDE_ARGS[@]}"} safely expands an empty array
# under set -u (avoids "unbound variable" in bash < 4.4).
GIT_INDEX_FILE="$TEMP_INDEX" git -C "$REPO_PATH" ${EXCLUDE_ARGS[@]+"${EXCLUDE_ARGS[@]}"} add -A 2>/dev/null

# Create a tree object from the temp index
TREE="$(GIT_INDEX_FILE="$TEMP_INDEX" git -C "$REPO_PATH" write-tree)"

#-------------------------------------------------------------------------------
# Idempotency check: skip if tree is unchanged since last sync
#-------------------------------------------------------------------------------

# Use --verify to ensure rev-parse returns a valid SHA or fails cleanly.
# Without --verify, rev-parse may echo the literal ref string on failure.
LAST_TREE=""
if git -C "$REPO_PATH" rev-parse --verify "refs/heads/${SYNC_BRANCH}^{tree}" > /dev/null 2>&1; then
    LAST_TREE="$(git -C "$REPO_PATH" rev-parse --verify "refs/heads/${SYNC_BRANCH}^{tree}")"
fi

if [[ "$TREE" == "$LAST_TREE" ]]; then
    log "No changes detected, skipping"
    exit 0
fi

#-------------------------------------------------------------------------------
# Create commit on sync branch without switching branches
#-------------------------------------------------------------------------------

# Build commit message with timestamp and current branch context
CURRENT_BRANCH="$(git -C "$REPO_PATH" branch --show-current 2>/dev/null)" || CURRENT_BRANCH="detached"
[[ -z "$CURRENT_BRANCH" ]] && CURRENT_BRANCH="detached"
TIMESTAMP="$(date -Iseconds)"
COMMIT_MSG="sync: ${TIMESTAMP} on ${CURRENT_BRANCH}"

# Determine parent commit. Use --verify so we get a proper SHA or failure.
PARENT=""
if git -C "$REPO_PATH" rev-parse --verify "refs/heads/${SYNC_BRANCH}" > /dev/null 2>&1; then
    PARENT="$(git -C "$REPO_PATH" rev-parse --verify "refs/heads/${SYNC_BRANCH}")"
fi

# Create the commit object directly -- no branch checkout required
if [[ -n "$PARENT" ]]; then
    COMMIT="$(printf '%s\n' "$COMMIT_MSG" | git -C "$REPO_PATH" commit-tree "$TREE" -p "$PARENT")"
else
    COMMIT="$(printf '%s\n' "$COMMIT_MSG" | git -C "$REPO_PATH" commit-tree "$TREE")"
fi

# Update the sync branch ref to point to our new commit
git -C "$REPO_PATH" update-ref "refs/heads/${SYNC_BRANCH}" "$COMMIT"

log "Created commit $COMMIT (tree: ${TREE:0:12})"

#-------------------------------------------------------------------------------
# Push to remote
#-------------------------------------------------------------------------------

if [[ "$DRY_RUN" == "1" ]]; then
    log "Dry run -- skipping push"
else
    if git -C "$REPO_PATH" push "$REMOTE" "$SYNC_BRANCH" --force --quiet 2>/dev/null; then
        log "Pushed to ${REMOTE}/${SYNC_BRANCH}"
    else
        # Push failure is not fatal -- the local sync branch is still updated.
        # The next run will retry the push.
        log "WARNING: Push to ${REMOTE}/${SYNC_BRANCH} failed (will retry next cycle)"
    fi
fi

exit 0
