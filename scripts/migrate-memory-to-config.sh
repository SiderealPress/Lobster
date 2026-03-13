#!/bin/bash
# migrate-memory-to-config.sh
#
# Migrates canonical memory from ~/lobster-workspace/memory/ to ~/lobster-config/memory/
# as part of the lobster-config split (issue #201).
#
# Separates identity data (handoff, priorities, people, projects) from runtime data
# (logs, scheduled-jobs, event log) so that ~/lobster-config/ is portable and
# ~/lobster-workspace/ is ephemeral.
#
# Safe to run multiple times — idempotent.
#
# Usage:
#   bash scripts/migrate-memory-to-config.sh [--dry-run]

set -euo pipefail

WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
DRY_RUN=false

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
    esac
done

if $DRY_RUN; then
    echo "[dry-run] No files will be moved."
fi

echo ""
echo "=== Lobster Memory Migration (issue #201) ==="
echo "  Source:      $WORKSPACE_DIR/memory/"
echo "  Destination: $CONFIG_DIR/memory/"
echo ""

SRC="$WORKSPACE_DIR/memory"
DEST="$CONFIG_DIR/memory"

# Nothing to do if source doesn't exist
if [ ! -d "$SRC" ]; then
    info "Source $SRC does not exist — nothing to migrate."
    exit 0
fi

# Ensure destination structure exists
for dir in \
    "$DEST/canonical" \
    "$DEST/canonical/people" \
    "$DEST/canonical/projects" \
    "$DEST/archive/digests"; do
    if [ ! -d "$dir" ]; then
        if $DRY_RUN; then
            info "[dry-run] Would create directory: $dir"
        else
            mkdir -p "$dir"
            success "Created: $dir"
        fi
    fi
done

moved=0
skipped=0

# Pure function: move a single file if not already at destination
move_file() {
    local src_file="$1"
    local dest_file="$2"

    if [ ! -f "$src_file" ]; then
        return
    fi

    if [ -f "$dest_file" ]; then
        info "  Already exists, skipping: $(basename "$src_file")"
        skipped=$((skipped + 1))
        return
    fi

    if $DRY_RUN; then
        info "  [dry-run] Would move: $src_file -> $dest_file"
    else
        mv "$src_file" "$dest_file"
        success "  Moved: $(basename "$src_file")"
    fi
    moved=$((moved + 1))
}

# Pure function: move all files from one directory to another
move_dir_contents() {
    local src_dir="$1"
    local dest_dir="$2"
    local label="$3"

    if [ ! -d "$src_dir" ]; then
        return
    fi

    info "Migrating $label..."
    for f in "$src_dir"/*; do
        [ -e "$f" ] || continue
        local fname
        fname=$(basename "$f")
        if [ -f "$f" ]; then
            move_file "$f" "$dest_dir/$fname"
        elif [ -d "$f" ]; then
            # Recursively handle subdirectories
            mkdir -p "$dest_dir/$fname" 2>/dev/null || true
            move_dir_contents "$f" "$dest_dir/$fname" "$label/$fname"
        fi
    done
}

# Migrate canonical root files (handoff.md, priorities.md, etc.)
move_dir_contents "$SRC/canonical" "$DEST/canonical" "canonical"

# Migrate archive/digests
move_dir_contents "$SRC/archive/digests" "$DEST/archive/digests" "archive/digests"

# Clean up empty source directories (leave non-empty ones alone)
cleanup_empty() {
    local dir="$1"
    if [ -d "$dir" ] && [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
        if $DRY_RUN; then
            info "[dry-run] Would remove empty dir: $dir"
        else
            rmdir "$dir" 2>/dev/null && info "Removed empty dir: $dir" || true
        fi
    fi
}

cleanup_empty "$SRC/canonical/people"
cleanup_empty "$SRC/canonical/projects"
cleanup_empty "$SRC/canonical"
cleanup_empty "$SRC/archive/digests"
cleanup_empty "$SRC/archive"
cleanup_empty "$SRC"

echo ""
echo "=== Migration Summary ==="
echo "  Moved:   $moved file(s)"
echo "  Skipped: $skipped file(s) (already at destination)"
echo ""

if $DRY_RUN; then
    echo "[dry-run] Run without --dry-run to apply changes."
else
    success "Migration complete. Canonical memory is now in $DEST"
fi
