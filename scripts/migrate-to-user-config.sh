#!/bin/bash
#===============================================================================
# Migrate to lobster-user-config layout
#
# Idempotent migration script that moves canonical memory and user context files
# from lobster-workspace/ to the new lobster-user-config/ directory structure.
#
# New layout:
#   ~/lobster-user-config/
#     memory/
#       canonical/          <- was ~/lobster-workspace/memory/canonical/
#     agents/
#       base.bootup.md      <- was ~/lobster-workspace/.claude/user.md (behavioral)
#       base.context.md     <- new file for personal facts (stub if not present)
#       dispatcher.bootup.md <- was ~/lobster-workspace/.claude/dispatcher.md
#       subagent.bootup.md  <- was ~/lobster-workspace/.claude/subagent.md
#       subagents/          <- empty dir for user-defined custom subagents
#
# Safe to run multiple times (idempotent).
# Does not delete original files — moves are copies only.
#===============================================================================

set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
step()    { echo -e "\n${CYAN}${BOLD}--- $* ---${NC}"; }

WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
USER_CONFIG_DIR="${LOBSTER_USER_CONFIG:-$HOME/lobster-user-config}"
OLD_CLAUDE_DIR="$WORKSPACE_DIR/.claude"
INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
TEMPLATES_DIR="$INSTALL_DIR/memory/canonical-templates"

echo ""
echo -e "${CYAN}${BOLD}Lobster: Migrate to lobster-user-config${NC}"
echo "========================================"
echo ""
info "Workspace:   $WORKSPACE_DIR"
info "User config: $USER_CONFIG_DIR"
echo ""

MIGRATED=0

#===============================================================================
# Step 1: Create directory structure
#===============================================================================

step "Creating lobster-user-config directory structure..."

mkdir -p "$USER_CONFIG_DIR/memory/canonical/people"
mkdir -p "$USER_CONFIG_DIR/memory/canonical/projects"
mkdir -p "$USER_CONFIG_DIR/memory/archive/digests"
mkdir -p "$USER_CONFIG_DIR/agents/subagents"

success "Directory structure ready"

#===============================================================================
# Step 2: Migrate canonical memory from workspace to user-config
#===============================================================================

step "Migrating canonical memory files..."

OLD_CANONICAL="$WORKSPACE_DIR/memory/canonical"
NEW_CANONICAL="$USER_CONFIG_DIR/memory/canonical"

if [ -d "$OLD_CANONICAL" ]; then
    # Count .md files in old location
    old_count=$(find "$OLD_CANONICAL" -name '*.md' 2>/dev/null | wc -l)
    if [ "$old_count" -gt 0 ]; then
        # Check destination so we don't overwrite existing content
        new_count=$(find "$NEW_CANONICAL" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l)
        if [ "$new_count" -gt 0 ]; then
            info "Destination already has $new_count .md files — skipping top-level migration (idempotent)"
        else
            info "Copying $old_count canonical files..."
            # Copy top-level .md files
            for f in "$OLD_CANONICAL"/*.md; do
                [ -f "$f" ] || continue
                base=$(basename "$f")
                dest="$NEW_CANONICAL/$base"
                if [ ! -f "$dest" ]; then
                    cp "$f" "$dest"
                    info "  Copied: $base"
                    MIGRATED=$((MIGRATED + 1))
                else
                    info "  Skipped (exists): $base"
                fi
            done
        fi
        # Copy subdirectories (always try, skipping existing files)
        for subdir in people projects; do
            if [ -d "$OLD_CANONICAL/$subdir" ]; then
                mkdir -p "$NEW_CANONICAL/$subdir"
                for f in "$OLD_CANONICAL/$subdir"/*.md; do
                    [ -f "$f" ] || continue
                    base=$(basename "$f")
                    dest="$NEW_CANONICAL/$subdir/$base"
                    if [ ! -f "$dest" ]; then
                        cp "$f" "$dest"
                        info "  Copied: $subdir/$base"
                        MIGRATED=$((MIGRATED + 1))
                    fi
                done
            fi
        done
        success "Canonical memory migration complete"
    else
        info "No canonical memory files found in old location"
    fi
else
    info "Old canonical dir not found: $OLD_CANONICAL (skip)"
fi

# Also migrate archive/digests if they exist
OLD_ARCHIVE="$WORKSPACE_DIR/memory/archive/digests"
NEW_ARCHIVE="$USER_CONFIG_DIR/memory/archive/digests"
if [ -d "$OLD_ARCHIVE" ]; then
    for f in "$OLD_ARCHIVE"/*.md "$OLD_ARCHIVE"/*.json 2>/dev/null; do
        [ -f "$f" ] || continue
        base=$(basename "$f")
        dest="$NEW_ARCHIVE/$base"
        if [ ! -f "$dest" ]; then
            mkdir -p "$NEW_ARCHIVE"
            cp "$f" "$dest"
            MIGRATED=$((MIGRATED + 1))
        fi
    done
fi

#===============================================================================
# Step 3: Migrate .claude/ user context files to agents/
#===============================================================================

step "Migrating .claude/ user context files..."

NEW_AGENTS="$USER_CONFIG_DIR/agents"

# Migrate user.md -> base.bootup.md
if [ -f "$OLD_CLAUDE_DIR/user.md" ]; then
    dest="$NEW_AGENTS/base.bootup.md"
    if [ ! -s "$dest" ]; then
        cp "$OLD_CLAUDE_DIR/user.md" "$dest"
        success "Migrated: .claude/user.md -> agents/base.bootup.md"
        MIGRATED=$((MIGRATED + 1))
    else
        info "agents/base.bootup.md already populated — skipping"
    fi
else
    info ".claude/user.md not found — skipping"
fi

# Migrate dispatcher.md -> dispatcher.bootup.md
if [ -f "$OLD_CLAUDE_DIR/dispatcher.md" ]; then
    dest="$NEW_AGENTS/dispatcher.bootup.md"
    if [ ! -s "$dest" ]; then
        cp "$OLD_CLAUDE_DIR/dispatcher.md" "$dest"
        success "Migrated: .claude/dispatcher.md -> agents/dispatcher.bootup.md"
        MIGRATED=$((MIGRATED + 1))
    else
        info "agents/dispatcher.bootup.md already populated — skipping"
    fi
else
    info ".claude/dispatcher.md not found — skipping"
fi

# Migrate subagent.md -> subagent.bootup.md
if [ -f "$OLD_CLAUDE_DIR/subagent.md" ]; then
    dest="$NEW_AGENTS/subagent.bootup.md"
    if [ ! -s "$dest" ]; then
        cp "$OLD_CLAUDE_DIR/subagent.md" "$dest"
        success "Migrated: .claude/subagent.md -> agents/subagent.bootup.md"
        MIGRATED=$((MIGRATED + 1))
    else
        info "agents/subagent.bootup.md already populated — skipping"
    fi
else
    info ".claude/subagent.md not found — skipping"
fi

#===============================================================================
# Step 4: Create stub files for any missing agent files
#===============================================================================

step "Ensuring stub agent files exist..."

for stub in "base.bootup.md" "base.context.md" "dispatcher.bootup.md" "subagent.bootup.md"; do
    dest="$USER_CONFIG_DIR/agents/$stub"
    if [ ! -f "$dest" ]; then
        touch "$dest"
        info "Created stub: agents/$stub"
        MIGRATED=$((MIGRATED + 1))
    fi
done

#===============================================================================
# Step 5: Seed canonical templates if canonical dir is empty
#===============================================================================

step "Seeding canonical templates if needed..."

NEW_CANONICAL="$USER_CONFIG_DIR/memory/canonical"
if [ -d "$TEMPLATES_DIR" ]; then
    md_count=$(find "$NEW_CANONICAL" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l)
    if [ "$md_count" -eq 0 ]; then
        for tmpl in "$TEMPLATES_DIR"/*.md; do
            [ -f "$tmpl" ] || continue
            base=$(basename "$tmpl")
            [[ "$base" == example-* ]] && continue
            dest="$NEW_CANONICAL/$base"
            if [ ! -f "$dest" ]; then
                cp "$tmpl" "$dest"
                info "Seeded template: $base"
                MIGRATED=$((MIGRATED + 1))
            fi
        done
    else
        info "Canonical dir has $md_count files — skipping template seed"
    fi
fi

#===============================================================================
# Summary
#===============================================================================

echo ""
echo "========================================"
if [ "$MIGRATED" -eq 0 ]; then
    success "Already up to date — no changes needed"
else
    success "$MIGRATED file(s) migrated/created"
fi
echo ""
info "User config dir: $USER_CONFIG_DIR"
info "  memory/canonical/ — canonical memory (handoff, priorities, projects)"
info "  agents/           — behavioral overrides (base.bootup.md, base.context.md, etc.)"
echo ""
warn "Original files in $WORKSPACE_DIR/memory/ and $OLD_CLAUDE_DIR/ are preserved."
warn "You can remove them manually once you have verified the migration."
echo ""
