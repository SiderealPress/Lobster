#!/bin/bash
# push-canonical.sh -- Commit and push updated canonical memory files to GitHub
#
# Called after nightly consolidation to ensure the HTTP bridge and local
# clones always have up-to-date canonical data.
#
# Behaviour:
#   - Only commits if memory/canonical/ has uncommitted changes
#   - Idempotent: safe to call multiple times
#   - Exits 0 on success or no-op; non-zero on failure
#
# Usage:
#   scripts/push-canonical.sh              # run from repo root
#   PUSH_CANONICAL_DRY_RUN=1 scripts/push-canonical.sh  # dry-run mode

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOBSTER_DIR="$(dirname "$SCRIPT_DIR")"
CANONICAL_REL="memory/canonical"
DATE_STAMP="$(date +%Y-%m-%d)"
DRY_RUN="${PUSH_CANONICAL_DRY_RUN:-0}"

cd "$LOBSTER_DIR"

# -- Preflight checks --------------------------------------------------------

# Ensure we are on the main branch (or allow override)
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "main" ] && [ "${PUSH_CANONICAL_ALLOW_BRANCH:-0}" != "1" ]; then
    echo "[push-canonical] WARNING: not on main branch (on '$CURRENT_BRANCH'). Skipping push."
    exit 0
fi

# Ensure the canonical directory exists
if [ ! -d "$CANONICAL_REL" ]; then
    echo "[push-canonical] No canonical directory at $CANONICAL_REL. Nothing to push."
    exit 0
fi

# -- Check for changes -------------------------------------------------------

# Stage canonical files to detect both modified and new files
git add "$CANONICAL_REL"

if git diff --cached --quiet -- "$CANONICAL_REL"; then
    echo "[push-canonical] No canonical file changes to push."
    exit 0
fi

# -- Commit and push ----------------------------------------------------------

COMMIT_MSG="chore: nightly consolidation ${DATE_STAMP}"

if [ "$DRY_RUN" = "1" ]; then
    echo "[push-canonical] DRY RUN: would commit with message: $COMMIT_MSG"
    echo "[push-canonical] DRY RUN: changed files:"
    git diff --cached --name-only -- "$CANONICAL_REL"
    # Unstage so we don't leave things in a weird state
    git reset HEAD -- "$CANONICAL_REL" > /dev/null 2>&1
    exit 0
fi

git commit -m "$COMMIT_MSG"

if git push origin main; then
    echo "[push-canonical] Canonical files pushed to GitHub at $(date -Iseconds)"
else
    echo "[push-canonical] ERROR: git push failed. Changes are committed locally." >&2
    exit 1
fi
