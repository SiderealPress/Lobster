#!/bin/bash
#===============================================================================
# Lobster Upgrade Script
#
# For users who haven't updated in 3+ days. Handles everything needed to bring
# an existing Lobster installation up to date, including new features like
# conversation history, headless browser (fetch_page), and LobsterDrop.
#
# Usage: ~/lobster/scripts/upgrade.sh [OPTIONS]
#
# Options:
#   --help              Show this help message
#   --dry-run           Show what would happen without making changes
#   --skip-syncthing    Skip Syncthing/LobsterDrop setup prompt
#   --skip-playwright   Skip Playwright/Chromium installation
#   --force             Continue past non-critical errors
#
# Exit codes:
#   0 - Success
#   1 - General error
#   2 - Lock file exists (another upgrade running)
#   3 - Pre-flight check failed
#===============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# Directories
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
LOBSTER_CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
USER_CONFIG_DIR="${LOBSTER_USER_CONFIG:-$HOME/lobster-user-config}"
BACKUP_BASE="$HOME/lobster-backups"
CONFIG_FILE="$LOBSTER_CONFIG_DIR/config.env"
LOCK_FILE="/tmp/lobster-upgrade.lock"
VENV_DIR="$LOBSTER_DIR/.venv"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

# Options
DRY_RUN=false
SKIP_SYNCTHING=false
SKIP_PLAYWRIGHT=false
FORCE=false

# State
BACKUP_DIR=""
PREVIOUS_COMMIT=""
CURRENT_COMMIT=""
UPGRADE_LOG=""
ERRORS=0
WARNINGS=0

#===============================================================================
# Logging
#===============================================================================

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; WARNINGS=$((WARNINGS + 1)); }
error()   { echo -e "${RED}[ERR ]${NC} $*"; ERRORS=$((ERRORS + 1)); }
step()    { echo -e "\n${CYAN}${BOLD}--- $* ---${NC}"; }
substep() { echo -e "  ${MAGENTA}>>>${NC} $*"; }

log_to_file() {
    if [ -n "$UPGRADE_LOG" ] && [ -f "$UPGRADE_LOG" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$UPGRADE_LOG"
    fi
}

die() {
    error "$1"
    cleanup_lock
    exit "${2:-1}"
}

#===============================================================================
# Help
#===============================================================================

show_help() {
    cat <<'HELP'
Lobster Upgrade Script
======================

Brings an existing Lobster installation up to date. Safe to run multiple times.

Usage:
  ~/lobster/scripts/upgrade.sh [OPTIONS]

Options:
  --help              Show this help message and exit
  --dry-run           Preview changes without applying them
  --skip-syncthing    Skip Syncthing/LobsterDrop setup
  --skip-playwright   Skip Playwright/Chromium installation
  --force             Continue past non-critical errors

What it does:
  1. Backs up config, env files, tasks, and scheduled jobs
  2. Pulls latest code from main branch
  3. Updates Python dependencies in the venv
  4. Creates any new directories the updated code expects
  5. Optionally installs Syncthing for LobsterDrop file sharing
  6. Installs Playwright + Chromium for the fetch_page tool
  7. Reloads systemd service files if changed
  8. Restarts the Telegram bot and MCP server
  9. Migrates old config formats if detected
  10. Runs a health check to verify everything works

Examples:
  # Standard upgrade
  ~/lobster/scripts/upgrade.sh

  # Preview what would change
  ~/lobster/scripts/upgrade.sh --dry-run

  # Upgrade without Syncthing or Playwright prompts
  ~/lobster/scripts/upgrade.sh --skip-syncthing --skip-playwright
HELP
    exit 0
}

#===============================================================================
# Argument parsing
#===============================================================================

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h)          show_help ;;
            --dry-run)          DRY_RUN=true ;;
            --skip-syncthing)   SKIP_SYNCTHING=true ;;
            --skip-playwright)  SKIP_PLAYWRIGHT=true ;;
            --force)            FORCE=true ;;
            *)                  die "Unknown option: $1. Use --help for usage." 1 ;;
        esac
        shift
    done
}

#===============================================================================
# Lock management
#===============================================================================

acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local pid
        pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
        if [ "$pid" != "unknown" ] && kill -0 "$pid" 2>/dev/null; then
            die "Another upgrade is running (PID: $pid). Remove $LOCK_FILE if stale." 2
        else
            warn "Stale lock file found, removing..."
            rm -f "$LOCK_FILE"
        fi
    fi
    echo $$ > "$LOCK_FILE"
}

cleanup_lock() {
    rm -f "$LOCK_FILE"
}

#===============================================================================
# 0. Pre-flight checks
#===============================================================================

preflight_checks() {
    step "Pre-flight checks"

    # Lobster install must exist
    if [ ! -d "$LOBSTER_DIR" ]; then
        die "Lobster not found at $LOBSTER_DIR. Is Lobster installed?" 3
    fi

    # Detect install mode
    if [ -d "$LOBSTER_DIR/.git" ]; then
        INSTALL_MODE="git"
        success "Lobster repo found at $LOBSTER_DIR (git mode)"
    else
        INSTALL_MODE="tarball"
        INSTALLED_VERSION=$(cat "$LOBSTER_DIR/VERSION" 2>/dev/null || echo "0.0.0")
        success "Lobster found at $LOBSTER_DIR (tarball mode, v$INSTALLED_VERSION)"
    fi

    # Internet connectivity
    if ! curl -s --connect-timeout 5 https://api.github.com >/dev/null 2>&1; then
        die "No internet connectivity (cannot reach api.github.com)" 3
    fi
    success "Internet connectivity OK"

    # Disk space (need at least 200MB for Playwright + Chromium)
    local free_kb
    free_kb=$(df "$HOME" | awk 'NR==2 {print $4}')
    if [ "$free_kb" -lt 204800 ]; then
        warn "Low disk space ($(( free_kb / 1024 ))MB free). Chromium install may fail."
    else
        success "Disk space OK ($(( free_kb / 1024 ))MB free)"
    fi

    # Python venv
    if [ ! -d "$VENV_DIR" ]; then
        warn "Python venv not found at $VENV_DIR. Will attempt to create one."
    else
        success "Python venv found"
    fi

    # Record current version/commit
    cd "$LOBSTER_DIR"
    if [ "$INSTALL_MODE" = "git" ]; then
        PREVIOUS_COMMIT=$(git rev-parse --short HEAD)
        info "Current commit: $PREVIOUS_COMMIT"
    else
        PREVIOUS_COMMIT="v$INSTALLED_VERSION"
        info "Current version: $INSTALLED_VERSION"
    fi
}

#===============================================================================
# 1. Backup
#===============================================================================

backup_config() {
    step "Backing up current configuration"

    local timestamp
    timestamp=$(date '+%Y%m%d-%H%M%S')
    BACKUP_DIR="$BACKUP_BASE/upgrade-$timestamp"
    UPGRADE_LOG="$BACKUP_BASE/upgrade-$timestamp.log"

    if $DRY_RUN; then
        info "[dry-run] Would create backup at $BACKUP_DIR"
        return 0
    fi

    mkdir -p "$BACKUP_DIR"
    echo "Upgrade started at $(date)" > "$UPGRADE_LOG"

    # Config files
    local files_to_backup=(
        "$LOBSTER_CONFIG_DIR/config.env"
        "$LOBSTER_CONFIG_DIR/lobster.conf"
        "$LOBSTER_CONFIG_DIR/sync-repos.json"
        "$LOBSTER_DIR/config/config.env"
        "$LOBSTER_DIR/config/lobster.conf"
        "$WORKSPACE_DIR/scheduled-jobs/jobs.json"
        "$MESSAGES_DIR/tasks.json"
        "$WORKSPACE_DIR/.lobster_session_id"
        "$WORKSPACE_DIR/CLAUDE.md"
    )

    local backed_up=0
    for file in "${files_to_backup[@]}"; do
        if [ -f "$file" ]; then
            local rel_path="${file#$HOME/}"
            local dest="$BACKUP_DIR/$rel_path"
            mkdir -p "$(dirname "$dest")"
            cp "$file" "$dest"
            substep "Backed up: $rel_path"
            backed_up=$((backed_up + 1))
        fi
    done

    # Backup .env files (catch any variant)
    for env_file in "$LOBSTER_DIR"/.env* "$LOBSTER_DIR"/config/*.env; do
        if [ -f "$env_file" ]; then
            local rel_path="${env_file#$HOME/}"
            local dest="$BACKUP_DIR/$rel_path"
            mkdir -p "$(dirname "$dest")"
            cp "$env_file" "$dest"
            substep "Backed up: $rel_path"
            backed_up=$((backed_up + 1))
        fi
    done

    # Backup systemd service files if they exist
    for svc in lobster-router lobster-claude lobster-slack-router; do
        if [ -f "/etc/systemd/system/${svc}.service" ]; then
            cp "/etc/systemd/system/${svc}.service" "$BACKUP_DIR/${svc}.service" 2>/dev/null || true
        fi
    done

    # Save git state
    echo "$PREVIOUS_COMMIT" > "$BACKUP_DIR/git-commit.txt"
    cd "$LOBSTER_DIR" && git log --oneline -5 > "$BACKUP_DIR/git-log.txt" 2>/dev/null || true

    success "Backup complete ($backed_up files) at $BACKUP_DIR"
    log_to_file "Backup created at $BACKUP_DIR with $backed_up files"
}

#===============================================================================
# 2. Git pull
#===============================================================================

git_pull() {
    if [ "$INSTALL_MODE" = "tarball" ]; then
        tarball_update
        return $?
    fi

    step "Pulling latest code from main"

    cd "$LOBSTER_DIR"

    # Stash local changes if any
    if [ -n "$(git status --porcelain)" ]; then
        if $DRY_RUN; then
            info "[dry-run] Would stash local changes"
        else
            warn "Local changes detected, stashing..."
            git stash push -m "lobster-upgrade-$(date +%Y%m%d-%H%M%S)" --quiet
            success "Local changes stashed"
        fi
    fi

    # Ensure we're on main before pulling.
    # If ~/lobster/ is on a feature branch or detached HEAD (e.g. after local
    # testing), git merge --ff-only would fail. Switch to main first so the
    # update always lands correctly.
    local current_branch
    current_branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "DETACHED")
    if [ "$current_branch" != "main" ]; then
        if $DRY_RUN; then
            info "[dry-run] Not on main branch (currently: $current_branch). Would switch to main before updating."
        else
            warn "Not on main branch (currently: $current_branch). Switching to main before updating..."
            git checkout main --quiet || die "Could not checkout main. Resolve manually and re-run." 3
            success "Switched to main"
        fi
    fi

    # Fetch
    info "Fetching from origin..."
    if $DRY_RUN; then
        git fetch origin main --quiet 2>/dev/null || die "Failed to fetch from origin" 3
        local behind
        behind=$(git rev-list HEAD..origin/main --count 2>/dev/null || echo "0")
        info "[dry-run] $behind commit(s) available"
        CURRENT_COMMIT=$(git rev-parse --short origin/main)
        info "[dry-run] Would update to: $CURRENT_COMMIT"
        return 0
    fi

    git fetch origin main --quiet 2>/dev/null || die "Failed to fetch from origin" 3

    local behind
    behind=$(git rev-list HEAD..origin/main --count 2>/dev/null || echo "0")

    if [ "$behind" = "0" ]; then
        success "Already up to date"
        CURRENT_COMMIT="$PREVIOUS_COMMIT"
    else
        info "$behind commit(s) to pull"
        if git merge origin/main --ff-only --quiet 2>/dev/null; then
            CURRENT_COMMIT=$(git rev-parse --short HEAD)
            success "Updated: $PREVIOUS_COMMIT -> $CURRENT_COMMIT"

            # Show changes
            info "Recent changes:"
            git log --oneline "$PREVIOUS_COMMIT..$CURRENT_COMMIT" 2>/dev/null | while read -r line; do
                echo "    $line"
            done
        else
            warn "Fast-forward merge failed. Attempting rebase..."
            if git rebase origin/main --quiet 2>/dev/null; then
                CURRENT_COMMIT=$(git rev-parse --short HEAD)
                success "Rebased to: $CURRENT_COMMIT"
            else
                git rebase --abort 2>/dev/null || true
                die "Could not update repo. Manual intervention needed." 1
            fi
        fi
    fi

    # Abort if health-check script has syntax errors
    if ! bash -n scripts/health-check-v3.sh; then
        echo "ERROR: scripts/health-check-v3.sh failed syntax check — aborting upgrade" >&2
        exit 1
    fi

    log_to_file "Git updated: $PREVIOUS_COMMIT -> $CURRENT_COMMIT"
}

#===============================================================================
# 2-alt. Tarball update (for non-git installs)
#===============================================================================

tarball_update() {
    step "Checking GitHub Releases for updates"

    local api_url="https://api.github.com/repos/SiderealPress/lobster/releases/latest"
    local release_json
    release_json=$(curl -fsSL "$api_url" 2>/dev/null) || die "Failed to fetch latest release" 3

    local latest_tag
    latest_tag=$(echo "$release_json" | jq -r '.tag_name // empty')
    if [ -z "$latest_tag" ]; then
        die "Could not parse latest release tag" 3
    fi

    local latest_version="${latest_tag#v}"
    PREVIOUS_COMMIT="v$INSTALLED_VERSION"

    if [ "$latest_version" = "$INSTALLED_VERSION" ]; then
        success "Already up to date (v$INSTALLED_VERSION)"
        CURRENT_COMMIT="$PREVIOUS_COMMIT"
        return 0
    fi

    info "Update available: v$INSTALLED_VERSION -> v$latest_version"

    # Find tarball asset (prefer our custom one, fall back to GitHub auto-tarball)
    local tarball_url
    tarball_url=$(echo "$release_json" | jq -r '.assets[] | select(.name | test("lobster.*\\.tar\\.gz")) | .browser_download_url' | head -1)
    if [ -z "$tarball_url" ]; then
        tarball_url=$(echo "$release_json" | jq -r '.tarball_url // empty')
    fi

    if [ -z "$tarball_url" ]; then
        die "No tarball URL found in release" 3
    fi

    if $DRY_RUN; then
        info "[dry-run] Would download: $tarball_url"
        info "[dry-run] Would swap $LOBSTER_DIR with new version"
        CURRENT_COMMIT="v$latest_version"
        return 0
    fi

    # Download tarball
    local tmp_dir
    tmp_dir=$(mktemp -d -t lobster-upgrade-XXXXXX)
    local tarball_file="$tmp_dir/lobster.tar.gz"

    substep "Downloading v$latest_version..."
    curl -fsSL -o "$tarball_file" "$tarball_url" || die "Failed to download tarball" 3
    success "Downloaded $(du -h "$tarball_file" | cut -f1)"

    # Verify checksum if available
    local checksum_url
    checksum_url=$(echo "$release_json" | jq -r '.assets[] | select(.name | test("checksums|sha256")) | .browser_download_url' | head -1)
    if [ -n "$checksum_url" ]; then
        substep "Verifying checksum..."
        local expected_checksum
        expected_checksum=$(curl -fsSL "$checksum_url" 2>/dev/null | head -1 | awk '{print $1}')
        local actual_checksum
        actual_checksum=$(sha256sum "$tarball_file" | awk '{print $1}')
        if [ -n "$expected_checksum" ] && [ "$expected_checksum" != "$actual_checksum" ]; then
            rm -rf "$tmp_dir"
            die "Checksum mismatch: expected $expected_checksum, got $actual_checksum" 3
        fi
        success "Checksum verified"
    fi

    # Extract tarball
    substep "Extracting..."
    local extract_dir="$tmp_dir/extracted"
    mkdir -p "$extract_dir"
    tar xzf "$tarball_file" -C "$extract_dir"

    # Find the extracted directory (GitHub wraps in owner-repo-sha/)
    local new_install
    new_install=$(find "$extract_dir" -maxdepth 1 -mindepth 1 -type d | head -1)
    if [ -z "$new_install" ]; then
        new_install="$extract_dir"
    fi

    # Preserve .venv from current install
    if [ -d "$LOBSTER_DIR/.venv" ]; then
        substep "Preserving Python venv..."
        mv "$LOBSTER_DIR/.venv" "$new_install/.venv"
    fi

    # Preserve .state directory
    if [ -d "$LOBSTER_DIR/.state" ]; then
        mv "$LOBSTER_DIR/.state" "$new_install/.state"
    fi

    # Swap directories
    local backup_dir="$HOME/lobster.bak"
    [ -d "$backup_dir" ] && rm -rf "$backup_dir"

    substep "Swapping install directory..."
    mv "$LOBSTER_DIR" "$backup_dir"
    mv "$new_install" "$LOBSTER_DIR"

    # Make scripts executable
    chmod +x "$LOBSTER_DIR/scripts/"*.sh 2>/dev/null || true
    chmod +x "$LOBSTER_DIR/install.sh" 2>/dev/null || true

    CURRENT_COMMIT="v$latest_version"
    success "Updated: v$INSTALLED_VERSION -> v$latest_version"

    # Cleanup
    rm -rf "$tmp_dir"

    log_to_file "Tarball updated: v$INSTALLED_VERSION -> v$latest_version"
}

#===============================================================================
# 2b. Show what's new (human-readable changelog)
#===============================================================================

show_whats_new() {
    local whatsnew_file="$LOBSTER_DIR/WHATSNEW"

    # Only show if we actually pulled new commits
    if [ "$PREVIOUS_COMMIT" = "$CURRENT_COMMIT" ]; then
        return 0
    fi

    # Only show if the WHATSNEW file exists in the new version
    if [ ! -f "$whatsnew_file" ]; then
        return 0
    fi

    # Check if WHATSNEW existed before this upgrade
    if git show "$PREVIOUS_COMMIT:WHATSNEW" &>/dev/null; then
        # Show only lines added since the user's last version
        local new_entries
        new_entries=$(diff --new-line-format='%L' --old-line-format='' --unchanged-line-format='' \
            <(git show "$PREVIOUS_COMMIT:WHATSNEW" 2>/dev/null) \
            "$whatsnew_file" 2>/dev/null || true)

        if [ -n "$new_entries" ]; then
            echo ""
            echo -e "${YELLOW}${BOLD}  What's new since your last update:${NC}"
            echo -e "${DIM}  ─────────────────────────────────────${NC}"
            echo "$new_entries" | grep -E '^### ' | sed 's/^### //' | while read -r entry; do
                echo -e "  ${GREEN}*${NC} $entry"
            done
            echo ""
            # Show full details
            echo "$new_entries" | grep -v '^#' | grep -v '^$' | while read -r line; do
                echo -e "    ${DIM}$line${NC}"
            done
            echo ""
        fi
    else
        # First time seeing WHATSNEW — show everything
        echo ""
        echo -e "${YELLOW}${BOLD}  Here's what Lobster can do now:${NC}"
        echo -e "${DIM}  ─────────────────────────────────────${NC}"
        grep -E '^### ' "$whatsnew_file" | sed 's/^### //' | while read -r entry; do
            echo -e "  ${GREEN}*${NC} $entry"
        done
        echo ""
        grep -v '^#' "$whatsnew_file" | grep -v '^$' | while read -r line; do
            echo -e "    ${DIM}$line${NC}"
        done
        echo ""
    fi
}

#===============================================================================
# 3. Python dependencies
#===============================================================================

update_python_deps() {
    step "Updating Python dependencies"

    cd "$LOBSTER_DIR"

    if $DRY_RUN; then
        info "[dry-run] Would update pip packages in venv"
        return 0
    fi

    # Create venv if missing
    if [ ! -d "$VENV_DIR" ]; then
        info "Creating Python virtual environment..."
        python3 -m venv "$VENV_DIR"
        success "venv created"
    fi

    # Activate and update
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"

    substep "Upgrading pip..."
    pip install --quiet --upgrade pip 2>/dev/null || true

    # Install from requirements.txt if it exists, otherwise install known deps
    if [ -f "$LOBSTER_DIR/requirements.txt" ]; then
        substep "Installing from requirements.txt..."
        pip install --quiet --upgrade -r "$LOBSTER_DIR/requirements.txt" 2>/dev/null || {
            warn "requirements.txt install had errors, installing core deps individually..."
            pip install --quiet --upgrade mcp python-telegram-bot watchdog python-dotenv 2>/dev/null || true
        }
    else
        substep "No requirements.txt found, installing core dependencies..."
        pip install --quiet --upgrade mcp python-telegram-bot watchdog python-dotenv 2>/dev/null || true
    fi

    # Always ensure playwright is importable (needed for fetch_page)
    if ! $SKIP_PLAYWRIGHT; then
        substep "Ensuring playwright is installed in venv..."
        pip install --quiet --upgrade playwright 2>/dev/null || warn "Failed to pip install playwright"
    fi

    deactivate
    success "Python dependencies updated"
    log_to_file "Python dependencies updated"
}

#===============================================================================
# 4. New directories
#===============================================================================

create_new_directories() {
    step "Creating new directories"

    local dirs_to_create=(
        "$MESSAGES_DIR/inbox"
        "$MESSAGES_DIR/outbox"
        "$MESSAGES_DIR/processed"
        "$MESSAGES_DIR/processing"
        "$MESSAGES_DIR/failed"
        "$MESSAGES_DIR/sent"
        "$MESSAGES_DIR/files"
        "$MESSAGES_DIR/images"
        "$MESSAGES_DIR/audio"
        "$MESSAGES_DIR/config"
        "$MESSAGES_DIR/task-outputs"
        "$WORKSPACE_DIR/scheduled-jobs/tasks"
        "$WORKSPACE_DIR/data"
        "$WORKSPACE_DIR/scheduled-jobs/logs"
        "$WORKSPACE_DIR/reports"
        "$USER_CONFIG_DIR/memory/canonical/people"
        "$USER_CONFIG_DIR/memory/canonical/projects"
        "$USER_CONFIG_DIR/memory/archive/digests"
        "$USER_CONFIG_DIR/agents/subagents"
    )

    local created=0
    for dir in "${dirs_to_create[@]}"; do
        if [ ! -d "$dir" ]; then
            if $DRY_RUN; then
                info "[dry-run] Would create: $dir"
            else
                mkdir -p "$dir"
                substep "Created: $dir"
            fi
            created=$((created + 1))
        fi
    done

    if [ "$created" -eq 0 ]; then
        success "All directories already exist"
    else
        success "Created $created new director(ies)"
    fi

    log_to_file "Directory check complete, created $created new directories"
}

#===============================================================================
# 5. Syncthing / LobsterDrop (optional, prompted)
#===============================================================================

setup_syncthing() {
    step "Syncthing / LobsterDrop (file sharing)"

    if $SKIP_SYNCTHING; then
        info "Skipping Syncthing setup (--skip-syncthing)"
        return 0
    fi

    if $DRY_RUN; then
        info "[dry-run] Would prompt for Syncthing setup"
        return 0
    fi

    # Check if already installed and running
    if command -v syncthing &>/dev/null; then
        if systemctl --user is-active --quiet syncthing.service 2>/dev/null; then
            success "Syncthing already installed and running"
            return 0
        else
            info "Syncthing installed but not running as user service"
        fi
    fi

    # Prompt - this is the only interactive part
    echo ""
    echo -e "${YELLOW}${BOLD}LobsterDrop${NC} uses Syncthing to sync files between your phone/laptop and this server."
    echo -e "It requires setup on your client device too (Syncthing app)."
    echo ""
    if [ -t 0 ]; then
        read -r -p "$(echo -e "${CYAN}Install and configure Syncthing? [Y/n]:${NC} ")" response
    else
        info "No TTY detected — defaulting to install Syncthing. Use --skip-syncthing to suppress."
        response="y"
    fi
    echo ""

    if [[ -n "$response" && ! "$response" =~ ^[Yy]$ ]]; then
        info "Skipping Syncthing setup"
        return 0
    fi

    # Install Syncthing
    if ! command -v syncthing &>/dev/null; then
        substep "Installing Syncthing..."
        # Use the official Syncthing APT repo
        if [ ! -f /etc/apt/sources.list.d/syncthing.list ]; then
            sudo mkdir -p /etc/apt/keyrings
            curl -fsSL https://syncthing.net/release-key.gpg | sudo gpg --dearmor -o /etc/apt/keyrings/syncthing-archive-keyring.gpg 2>/dev/null || {
                warn "Failed to add Syncthing GPG key, trying apt directly..."
            }
            echo "deb [signed-by=/etc/apt/keyrings/syncthing-archive-keyring.gpg] https://apt.syncthing.net/ syncthing stable" | sudo tee /etc/apt/sources.list.d/syncthing.list >/dev/null
            sudo apt-get update -qq 2>/dev/null || true
        fi
        sudo apt-get install -y -qq syncthing 2>/dev/null || {
            # Fallback: install from snap or direct download
            warn "APT install failed, trying snap..."
            sudo snap install syncthing 2>/dev/null || {
                error "Could not install Syncthing. Install manually: https://syncthing.net/"
                return 0
            }
        }
        success "Syncthing installed"
    else
        success "Syncthing already installed"
    fi

    # Enable linger for user (so services run without active login)
    substep "Enabling linger for user $USER..."
    sudo loginctl enable-linger "$USER" 2>/dev/null || warn "Could not enable linger"

    # Create systemd user service
    local user_service_dir="$HOME/.config/systemd/user"
    mkdir -p "$user_service_dir"

    if [ ! -f "$user_service_dir/syncthing.service" ]; then
        substep "Creating systemd user service for Syncthing..."
        cat > "$user_service_dir/syncthing.service" <<'SVCEOF'
[Unit]
Description=Syncthing - Open Source Continuous File Synchronization
Documentation=man:syncthing(1)
After=network.target

[Service]
ExecStart=/usr/bin/syncthing serve --no-browser --no-restart --logflags=0
Restart=on-failure
RestartSec=10
SuccessExitStatus=3 4
RestartForceExitStatus=3 4

[Install]
WantedBy=default.target
SVCEOF
    fi

    # Reload, enable, and start
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable syncthing.service 2>/dev/null || true
    systemctl --user start syncthing.service 2>/dev/null || true

    sleep 2
    if systemctl --user is-active --quiet syncthing.service 2>/dev/null; then
        success "Syncthing running as user service"
    else
        warn "Syncthing service may not have started. Check: systemctl --user status syncthing"
    fi

    # Create the LobsterDrop shared folder
    local drop_dir="$HOME/LobsterDrop"
    mkdir -p "$drop_dir"
    success "LobsterDrop folder: $drop_dir"

    echo ""
    echo -e "${YELLOW}Next steps for LobsterDrop:${NC}"
    echo "  1. Access Syncthing GUI at http://localhost:8384"
    echo "  2. Add $drop_dir as a shared folder"
    echo "  3. Install Syncthing on your phone/laptop"
    echo "  4. Pair the devices and share the LobsterDrop folder"
    echo ""

    log_to_file "Syncthing setup complete"
}

#===============================================================================
# 6. Playwright / Chromium
#===============================================================================

install_playwright() {
    step "Playwright / Chromium (headless browser for fetch_page)"

    if $SKIP_PLAYWRIGHT; then
        info "Skipping Playwright setup (--skip-playwright)"
        return 0
    fi

    if $DRY_RUN; then
        info "[dry-run] Would install Playwright and Chromium"
        return 0
    fi

    # Check if Chromium is already installed for Playwright
    local pw_browsers_path="$HOME/.cache/ms-playwright"
    if [ -d "$pw_browsers_path" ] && ls "$pw_browsers_path"/chromium-* &>/dev/null 2>&1; then
        success "Playwright Chromium already installed"
        return 0
    fi

    # Ensure playwright pip package is installed
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"

    if ! python -c "import playwright" 2>/dev/null; then
        substep "Installing playwright Python package..."
        pip install --quiet playwright 2>/dev/null || {
            warn "Failed to install playwright pip package"
            deactivate
            return 0
        }
    fi

    # Install system dependencies for Chromium
    substep "Installing Chromium system dependencies..."
    sudo apt-get install -y -qq \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
        libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        libwayland-client0 2>/dev/null || {
        warn "Some Chromium dependencies may be missing"
    }

    # Install Chromium via Playwright
    substep "Installing Chromium browser (this may take a minute)..."
    python -m playwright install chromium 2>/dev/null || {
        warn "Playwright chromium install failed. fetch_page tool will not work."
        warn "Try manually: source $VENV_DIR/bin/activate && python -m playwright install chromium"
        deactivate
        return 0
    }

    deactivate
    success "Playwright + Chromium installed"
    log_to_file "Playwright and Chromium installed"
}

#===============================================================================
# 7. Service restarts
#===============================================================================

restart_services() {
    step "Restarting services"

    if $DRY_RUN; then
        info "[dry-run] Would restart lobster-router and lobster-claude"
        return 0
    fi

    local services=("lobster-router" "lobster-claude")

    for svc in "${services[@]}"; do
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            substep "Restarting $svc..."
            if sudo systemctl restart "$svc" 2>/dev/null; then
                sleep 2
                if systemctl is-active --quiet "$svc" 2>/dev/null; then
                    success "$svc restarted and running"
                else
                    warn "$svc restarted but may not be running. Check: systemctl status $svc"
                fi
            else
                warn "Failed to restart $svc"
            fi
        else
            info "$svc not enabled, skipping"
        fi
    done

    # Also restart slack router if it exists and is enabled
    if systemctl is-enabled --quiet "lobster-slack-router" 2>/dev/null; then
        substep "Restarting lobster-slack-router..."
        sudo systemctl restart "lobster-slack-router" 2>/dev/null || warn "Failed to restart slack router"
    fi

    log_to_file "Services restarted"
}

#===============================================================================
# 8. Systemd service updates
#===============================================================================

update_systemd_services() {
    step "Checking systemd service files"

    cd "$LOBSTER_DIR"

    if $DRY_RUN; then
        info "[dry-run] Would check for service file changes"
        return 0
    fi

    # Only update if service templates have changed since last commit
    local need_reload=false

    # Check for generated service files and see if templates are newer
    for template in services/*.service.template; do
        [ -f "$template" ] || continue
        local svc_name
        svc_name=$(basename "$template" .template)
        local installed="/etc/systemd/system/$svc_name"

        if [ -f "$installed" ]; then
            # Compare template modification time with installed
            if [ "$template" -nt "$installed" ]; then
                info "Service template updated: $svc_name"
                need_reload=true
            fi
        fi
    done

    if $need_reload; then
        substep "Reloading systemd daemon..."
        sudo systemctl daemon-reload 2>/dev/null || warn "Failed to reload systemd daemon"
        success "Systemd daemon reloaded"
    else
        success "Service files up to date"
    fi

    log_to_file "Systemd service check complete"
}

#===============================================================================
# 9. Migration checks
#===============================================================================

run_migrations() {
    step "Running migration checks"

    local migrated=0

    if $DRY_RUN; then
        info "[dry-run] Would check for needed migrations"
        return 0
    fi

    # Migration 0: Config from repo to ~/lobster-config/ (tarball-readiness)
    mkdir -p "$LOBSTER_CONFIG_DIR"
    if [ -f "$LOBSTER_DIR/config/config.env" ] && [ ! -f "$LOBSTER_CONFIG_DIR/config.env" ]; then
        substep "Migrating config.env to $LOBSTER_CONFIG_DIR/ ..."
        cp "$LOBSTER_DIR/config/config.env" "$LOBSTER_CONFIG_DIR/config.env"
        success "Config migrated to $LOBSTER_CONFIG_DIR/config.env"
        migrated=$((migrated + 1))
    fi
    if [ -f "$LOBSTER_DIR/config/lobster.conf" ] && [ ! -f "$LOBSTER_CONFIG_DIR/lobster.conf" ]; then
        cp "$LOBSTER_DIR/config/lobster.conf" "$LOBSTER_CONFIG_DIR/lobster.conf"
        substep "Migrated lobster.conf to $LOBSTER_CONFIG_DIR/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$LOBSTER_DIR/config/consolidation.conf" ] && [ ! -f "$LOBSTER_CONFIG_DIR/consolidation.conf" ]; then
        cp "$LOBSTER_DIR/config/consolidation.conf" "$LOBSTER_CONFIG_DIR/consolidation.conf"
        substep "Migrated consolidation.conf to $LOBSTER_CONFIG_DIR/"
        migrated=$((migrated + 1))
    fi
    if [ -f "$LOBSTER_DIR/config/sync-repos.json" ] && [ ! -f "$LOBSTER_CONFIG_DIR/sync-repos.json" ]; then
        cp "$LOBSTER_DIR/config/sync-repos.json" "$LOBSTER_CONFIG_DIR/sync-repos.json"
        substep "Migrated sync-repos.json to $LOBSTER_CONFIG_DIR/"
        migrated=$((migrated + 1))
    fi

    # Migration 1: Old config location (~/.lobster.env -> lobster-config/config.env)
    if [ -f "$HOME/.lobster.env" ] && [ ! -f "$CONFIG_FILE" ]; then
        substep "Migrating .lobster.env to $LOBSTER_CONFIG_DIR/config.env..."
        mkdir -p "$LOBSTER_CONFIG_DIR"
        cp "$HOME/.lobster.env" "$CONFIG_FILE"
        success "Config migrated from ~/.lobster.env"
        migrated=$((migrated + 1))
    fi

    # Migration 2: Old .env in repo root -> lobster-config/config.env
    if [ -f "$LOBSTER_DIR/.env" ] && [ ! -f "$CONFIG_FILE" ]; then
        substep "Migrating .env to $LOBSTER_CONFIG_DIR/config.env..."
        mkdir -p "$LOBSTER_CONFIG_DIR"
        cp "$LOBSTER_DIR/.env" "$CONFIG_FILE"
        success "Config migrated from .env"
        migrated=$((migrated + 1))
    fi

    # Migration 3: Lobster rename - detect and disable old service names
    for old_svc in hyperion-router hyperion-daemon hyperion-claude; do
        if systemctl is-enabled --quiet "$old_svc" 2>/dev/null; then
            warn "Old service '$old_svc' found. Disabling in favor of lobster-* services."
            sudo systemctl stop "$old_svc" 2>/dev/null || true
            sudo systemctl disable "$old_svc" 2>/dev/null || true
            migrated=$((migrated + 1))
        fi
    done

    # Migration 4: Old messages directory structure (flat -> subdirs)
    if [ -d "$MESSAGES_DIR" ] && [ ! -d "$MESSAGES_DIR/inbox" ]; then
        substep "Messages directory missing subdirectories, creating them..."
        mkdir -p "$MESSAGES_DIR"/{inbox,outbox,processed,processing,failed,sent,files,images,audio,config,task-outputs}
        migrated=$((migrated + 1))
    fi

    # Migration 5: tasks.json location (lobster dir -> messages dir)
    if [ -f "$LOBSTER_DIR/tasks.json" ] && [ ! -f "$MESSAGES_DIR/tasks.json" ]; then
        substep "Moving tasks.json to messages directory..."
        cp "$LOBSTER_DIR/tasks.json" "$MESSAGES_DIR/tasks.json"
        success "tasks.json migrated"
        migrated=$((migrated + 1))
    fi

    # Migration 6: Ensure sent directory exists for conversation history
    if [ ! -d "$MESSAGES_DIR/sent" ]; then
        mkdir -p "$MESSAGES_DIR/sent"
        substep "Created sent/ directory for conversation history"
        migrated=$((migrated + 1))
    fi

    # Migration 7: Move scheduled task definition files from repo to workspace
    local old_tasks_dir="$LOBSTER_DIR/scheduled-tasks/tasks"
    local new_tasks_dir="$WORKSPACE_DIR/scheduled-jobs/tasks"
    if [ -d "$old_tasks_dir" ] && ls "$old_tasks_dir"/*.md &>/dev/null 2>&1; then
        mkdir -p "$new_tasks_dir"
        local task_moved=0
        for task_file in "$old_tasks_dir"/*.md; do
            local base
            base=$(basename "$task_file")
            if [ ! -f "$new_tasks_dir/$base" ]; then
                cp "$task_file" "$new_tasks_dir/$base"
                substep "Migrated task file: $base"
                task_moved=$((task_moved + 1))
            fi
        done
        if [ "$task_moved" -gt 0 ]; then
            success "Migrated $task_moved task file(s) to workspace"
            migrated=$((migrated + task_moved))
        fi
    fi

    # Migration 8: Seed canonical templates if empty (now in lobster-user-config)
    local canonical_dir="$USER_CONFIG_DIR/memory/canonical"
    local templates_dir="$LOBSTER_DIR/memory/canonical-templates"
    if [ -d "$templates_dir" ] && [ -d "$canonical_dir" ]; then
        local md_count
        md_count=$(find "$canonical_dir" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l)
        if [ "$md_count" -eq 0 ]; then
            for tmpl in "$templates_dir"/*.md; do
                [ -f "$tmpl" ] || continue
                local base
                base=$(basename "$tmpl")
                [[ "$base" == example-* ]] && continue
                cp "$tmpl" "$canonical_dir/$base"
                substep "Seeded canonical template: $base"
                migrated=$((migrated + 1))
            done
        fi
    fi

    # Migration 9: Move canonical memory from workspace to lobster-user-config
    local old_canonical="$WORKSPACE_DIR/memory/canonical"
    local new_canonical="$USER_CONFIG_DIR/memory/canonical"
    if [ -d "$old_canonical" ] && [ "$(find "$old_canonical" -name '*.md' 2>/dev/null | wc -l)" -gt 0 ]; then
        # Check if new location is empty (avoid overwriting if already migrated)
        local new_count
        new_count=$(find "$new_canonical" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l)
        if [ "$new_count" -eq 0 ]; then
            substep "Migrating canonical memory from workspace to lobster-user-config..."
            mkdir -p "$new_canonical"/{people,projects}
            # Copy top-level .md files
            for f in "$old_canonical"/*.md; do
                [ -f "$f" ] || continue
                base=$(basename "$f")
                cp "$f" "$new_canonical/$base"
                substep "  Moved: $base"
                migrated=$((migrated + 1))
            done
            # Copy subdirectories
            for subdir in people projects; do
                if [ -d "$old_canonical/$subdir" ]; then
                    mkdir -p "$new_canonical/$subdir"
                    for f in "$old_canonical/$subdir"/*.md; do
                        [ -f "$f" ] || continue
                        base=$(basename "$f")
                        cp "$f" "$new_canonical/$subdir/$base"
                        substep "  Moved: $subdir/$base"
                        migrated=$((migrated + 1))
                    done
                fi
            done
            success "Canonical memory migrated to $new_canonical"
        fi
    fi

    # Migration 10: Rename bootup files to sys.*/user.* naming convention
    # Must run BEFORE Migration 11 (stub creation) so that existing populated files are
    # renamed into place before Migration 11 would create empty stubs at the new names.
    # System files (.claude/ in workspace): dispatcher.bootup.md -> sys.dispatcher.bootup.md, subagent.bootup.md -> sys.subagent.bootup.md
    local ws_claude_dir="$WORKSPACE_DIR/.claude"
    if [ -f "$ws_claude_dir/dispatcher.bootup.md" ] && [ ! -s "$ws_claude_dir/sys.dispatcher.bootup.md" ]; then
        mv "$ws_claude_dir/dispatcher.bootup.md" "$ws_claude_dir/sys.dispatcher.bootup.md"
        substep "Renamed .claude/dispatcher.bootup.md -> .claude/sys.dispatcher.bootup.md"
        migrated=$((migrated + 1))
    fi
    if [ -f "$ws_claude_dir/subagent.bootup.md" ] && [ ! -s "$ws_claude_dir/sys.subagent.bootup.md" ]; then
        mv "$ws_claude_dir/subagent.bootup.md" "$ws_claude_dir/sys.subagent.bootup.md"
        substep "Renamed .claude/subagent.bootup.md -> .claude/sys.subagent.bootup.md"
        migrated=$((migrated + 1))
    fi
    # User-config files: rename *.bootup.md -> user.*.bootup.md convention
    local agents_dir="$USER_CONFIG_DIR/agents"
    if [ -f "$agents_dir/base.bootup.md" ] && [ ! -s "$agents_dir/user.base.bootup.md" ]; then
        mv "$agents_dir/base.bootup.md" "$agents_dir/user.base.bootup.md"
        substep "Renamed agents/base.bootup.md -> agents/user.base.bootup.md"
        migrated=$((migrated + 1))
    fi
    if [ -f "$agents_dir/base.context.md" ] && [ ! -s "$agents_dir/user.base.context.md" ]; then
        mv "$agents_dir/base.context.md" "$agents_dir/user.base.context.md"
        substep "Renamed agents/base.context.md -> agents/user.base.context.md"
        migrated=$((migrated + 1))
    fi
    if [ -f "$agents_dir/dispatcher.bootup.md" ] && [ ! -s "$agents_dir/user.dispatcher.bootup.md" ]; then
        mv "$agents_dir/dispatcher.bootup.md" "$agents_dir/user.dispatcher.bootup.md"
        substep "Renamed agents/dispatcher.bootup.md -> agents/user.dispatcher.bootup.md"
        migrated=$((migrated + 1))
    fi
    if [ -f "$agents_dir/subagent.bootup.md" ] && [ ! -s "$agents_dir/user.subagent.bootup.md" ]; then
        mv "$agents_dir/subagent.bootup.md" "$agents_dir/user.subagent.bootup.md"
        substep "Renamed agents/subagent.bootup.md -> agents/user.subagent.bootup.md"
        migrated=$((migrated + 1))
    fi

    # Migration 11: Create stub agent files in lobster-user-config if missing
    # Runs after Migration 10 so that files renamed into place are not clobbered by empty stubs.
    mkdir -p "$USER_CONFIG_DIR/agents/subagents"
    for stub_file in "user.base.bootup.md" "user.base.context.md" "user.dispatcher.bootup.md" "user.subagent.bootup.md"; do
        stub_dest="$USER_CONFIG_DIR/agents/$stub_file"
        if [ ! -f "$stub_dest" ]; then
            touch "$stub_dest"
            substep "Created stub: agents/$stub_file"
            migrated=$((migrated + 1))
        fi
    done

    # Migration 12: Migrate .claude/ user context files from workspace to user-config
    local old_claude_dir="$WORKSPACE_DIR/.claude"
    local new_agents_dir="$USER_CONFIG_DIR/agents"
    if [ -d "$old_claude_dir" ]; then
        # Migrate user.md -> user.base.bootup.md (behavioral) if not already done
        if [ -f "$old_claude_dir/user.md" ] && [ ! -s "$new_agents_dir/user.base.bootup.md" ]; then
            cp "$old_claude_dir/user.md" "$new_agents_dir/user.base.bootup.md"
            substep "Migrated .claude/user.md -> lobster-user-config/agents/user.base.bootup.md"
            migrated=$((migrated + 1))
        fi
        # Migrate dispatcher.md -> user.dispatcher.bootup.md
        if [ -f "$old_claude_dir/dispatcher.md" ] && [ ! -s "$new_agents_dir/user.dispatcher.bootup.md" ]; then
            cp "$old_claude_dir/dispatcher.md" "$new_agents_dir/user.dispatcher.bootup.md"
            substep "Migrated .claude/dispatcher.md -> lobster-user-config/agents/user.dispatcher.bootup.md"
            migrated=$((migrated + 1))
        fi
        # Migrate subagent.md -> user.subagent.bootup.md
        if [ -f "$old_claude_dir/subagent.md" ] && [ ! -s "$new_agents_dir/user.subagent.bootup.md" ]; then
            cp "$old_claude_dir/subagent.md" "$new_agents_dir/user.subagent.bootup.md"
            substep "Migrated .claude/subagent.md -> lobster-user-config/agents/user.subagent.bootup.md"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 13: Ensure health-check-v3.sh cron entry exists
    # Installs that set up the crontab manually before health-check-v3.sh was added
    # to install.sh may be missing the entry entirely, meaning no monitoring runs.
    local HEALTH_MARKER="# LOBSTER-HEALTH"
    if ! crontab -l 2>/dev/null | grep -q "$HEALTH_MARKER"; then
        local health_script="$LOBSTER_DIR/scripts/health-check-v3.sh"
        chmod +x "$health_script" 2>/dev/null || true
        ({ crontab -l 2>/dev/null | grep -v "health-check" || true; }; \
         echo "*/4 * * * * $health_script $HEALTH_MARKER") | crontab -
        substep "Added health-check-v3.sh to crontab (every 4 minutes)"
        migrated=$((migrated + 1))
    fi

    # Migration 14: Update health-check cron interval from */2 to */4
    # The stale-message threshold was raised from 3m to 4m to reduce false-positive
    # restarts from brief processing delays. Running the check every 4 minutes aligns
    # the cron interval with the new threshold so a single missed check cannot
    # immediately trigger a restart.
    if crontab -l 2>/dev/null | grep "$HEALTH_MARKER" | grep -q "\*/2"; then
        local health_script="$LOBSTER_DIR/scripts/health-check-v3.sh"
        ({ crontab -l 2>/dev/null | grep -v "$HEALTH_MARKER" | grep -v "health-check" || true; }; \
         echo "*/4 * * * * $health_script $HEALTH_MARKER") | crontab -
        substep "Updated health-check-v3.sh cron interval from */2 to */4"
        migrated=$((migrated + 1))
    fi

    # Migration 15: Remove orphan agents.db files — stale empty files not used by any code
    # (real session store is agent_sessions.db in ~/messages/config/ and ~/lobster-workspace/data/)
    if [ -f "$MESSAGES_DIR/config/agents.db" ]; then
        rm -f "$MESSAGES_DIR/config/agents.db"
        substep "Removed orphan agents.db from $MESSAGES_DIR/config/ (empty file, not used by any code)"
        migrated=$((migrated + 1))
    fi
    if [ -f "$WORKSPACE_DIR/data/agents.db" ]; then
        rm -f "$WORKSPACE_DIR/data/agents.db"
        substep "Removed orphan agents.db from $WORKSPACE_DIR/data/ (empty file, not used by any code)"
        migrated=$((migrated + 1))
    fi

    # Migration 16: Ensure messages/config/ directory exists for lobster-state.json
    # lobster-state.json lives in messages/config/ and is used by multiple features
    # (compaction suppression, boot grace period). This directory is created by
    # Migration 4 on new installs, but this step ensures it exists on any install
    # that skipped Migration 4 (e.g. manually provisioned or very old installs
    # where the directory may have been removed).
    if [ ! -d "$MESSAGES_DIR/config" ]; then
        mkdir -p "$MESSAGES_DIR/config"
        substep "Created $MESSAGES_DIR/config/ (required for lobster-state.json)"
        migrated=$((migrated + 1))
    fi

    # Migration 17: Ensure lobster-state.json has a booted_at field.
    # Fresh installs before this fix never wrote an initial lobster-state.json,
    # so is_boot_grace_period() in health-check-v3.sh always returned false on
    # first start — the grace window never applied and the health check fired
    # immediately, triggering a restart loop. We backfill booted_at only when
    # the field is absent; existing timestamps are left untouched.
    local state_json="$MESSAGES_DIR/config/lobster-state.json"
    if [ -f "$state_json" ]; then
        local has_booted_at
        has_booted_at=$(python3 -c "
import json, sys
try:
    d = json.load(open('$state_json'))
    print('yes' if 'booted_at' in d else 'no')
except Exception:
    print('no')
" 2>/dev/null)
        if [ "$has_booted_at" = "no" ]; then
            python3 -c "
import json, sys
from datetime import datetime, timezone
path = '$state_json'
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
try:
    with open(path) as f:
        d = json.load(f)
except Exception:
    d = {}
d['booted_at'] = now
with open(path, 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
" 2>/dev/null
            substep "Backfilled booted_at in lobster-state.json (fixes fresh-install restart loop)"
            migrated=$((migrated + 1))
        fi
    else
        # State file is absent entirely — create it so the next start has a grace period.
        echo '{"mode": "active", "booted_at": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}' > "$state_json"
        substep "Created lobster-state.json with initial booted_at (fixes fresh-install restart loop)"
        migrated=$((migrated + 1))
    fi

    # Migration 18: (superseded by Migration 21 — no-op, kept for numbering continuity)

    # Migration 19: Remove require-write-result.py from the Stop hook in settings.json
    # The Stop event fires for the dispatcher main session; SubagentStop fires for
    # Task-spawned subagents — they are mutually exclusive. The hook was incorrectly
    # registered under Stop (which hit the dispatcher) as well as SubagentStop.
    # The is_dispatcher() guard in the hook was a band-aid for this misregistration.
    # Fix: remove the entry from Stop[], leave it only under SubagentStop.
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local stop_has_write_result
        stop_has_write_result=$(jq -r '
            [.hooks.Stop[]?.hooks[]?.command // empty]
            | map(select(contains("require-write-result")))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${stop_has_write_result:-0}" != "0" ] && [ "${stop_has_write_result:-0}" != "" ]; then
            TMP_SETTINGS=$(mktemp)
            jq '
                .hooks.Stop = (
                    (.hooks.Stop // [])
                    | map(select(
                        (.hooks // [])
                        | map(.command // "")
                        | all(contains("require-write-result") | not)
                    ))
                )
            ' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Removed require-write-result.py from Stop hook (was mis-registered; SubagentStop entry kept)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 20: Fix sqlite-vec aarch64 ELFCLASS32 bug (0.1.6 ships a 32-bit ARM .so)
    # sqlite-vec 0.1.6 manylinux_aarch64 wheel incorrectly bundles a 32-bit ARM binary.
    # Installs that ran `uv sync` before this fix will have the broken wheel. Detect the
    # failure and reinstall to >=0.1.7a1 which ships a proper 64-bit aarch64 binary.
    if ! "$VENV_DIR/bin/python" -c \
        "import sqlite3, sqlite_vec; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c)" \
        2>/dev/null; then
        substep "sqlite-vec fails to load — reinstalling (fixes aarch64 ELFCLASS32 regression in 0.1.6)..."
        uv pip install --quiet "sqlite-vec>=0.1.7a1" 2>/dev/null || true
        if "$VENV_DIR/bin/python" -c \
            "import sqlite3, sqlite_vec; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c)" \
            2>/dev/null; then
            success "sqlite-vec reinstalled and loads correctly (semantic memory restored)"
            migrated=$((migrated + 1))
        else
            warn "sqlite-vec reinstall failed — semantic memory search will be unavailable"
        fi
    fi

    # Migration 21: Register missing system-file-protect and require-auditor-context-update hooks
    # install.sh used a fragile matcher-equality check (.matcher == "Edit|Write|NotebookEdit")
    # to detect if the hook was already installed. This check matched on the matcher string
    # rather than the command, so the hook was silently skipped on installs where settings.json
    # was created by Claude Code after install.sh ran. Both hooks are absent from live settings.json
    # on affected systems. Add them now if missing.
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        # Add system-file-protect PreToolUse hook if missing
        local has_file_protect
        has_file_protect=$(jq -r '
            [.hooks.PreToolUse[]?.hooks[]?.command // empty]
            | map(select(contains("system-file-protect")))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${has_file_protect:-0}" = "0" ] || [ "${has_file_protect:-0}" = "" ]; then
            TMP_SETTINGS=$(mktemp)
            jq --arg cmd "python3 $LOBSTER_DIR/hooks/system-file-protect.py" \
               '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
                "matcher": "Edit|Write|NotebookEdit",
                "hooks": [{
                    "type": "command",
                    "command": $cmd,
                    "timeout": 5
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered missing system-file-protect PreToolUse hook"
            migrated=$((migrated + 1))
        fi

        # Add require-auditor-context-update SubagentStop hook if missing
        local has_auditor
        has_auditor=$(jq -r '
            [.hooks.SubagentStop[]?.hooks[]?.command // empty]
            | map(select(contains("require-auditor-context-update")))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${has_auditor:-0}" = "0" ] || [ "${has_auditor:-0}" = "" ]; then
            TMP_SETTINGS=$(mktemp)
            jq --arg cmd "python3 $LOBSTER_DIR/hooks/require-auditor-context-update.py" \
               '.hooks.SubagentStop = (.hooks.SubagentStop // []) + [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": $cmd,
                    "timeout": 10
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered missing require-auditor-context-update SubagentStop hook"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 22: Ensure lobster-workspace/data/ directory exists for compaction-state.json
    # The compact_catchup agent writes last_compaction_ts to this file after each compaction.
    local data_dir="$WORKSPACE_DIR/data"
    if [ ! -d "$data_dir" ]; then
        mkdir -p "$data_dir"
        substep "Created $data_dir/ for compaction-state.json"
        migrated=$((migrated + 1))
    fi

    # Migration 23: Add stop_reason column to agent_sessions SQLite table
    # Existing rows will have NULL for stop_reason (nullable, backward-compatible).
    local DB_PATH="${LOBSTER_MESSAGES:-$HOME/messages}/config/agent_sessions.db"
    if [ -f "$DB_PATH" ]; then
        if ! sqlite3 "$DB_PATH" "PRAGMA table_info(agent_sessions);" 2>/dev/null | grep -q "stop_reason"; then
            substep "Adding stop_reason column to agent_sessions table..."
            sqlite3 "$DB_PATH" "ALTER TABLE agent_sessions ADD COLUMN stop_reason TEXT;" 2>/dev/null && \
                success "stop_reason column added to agent_sessions" || \
                warn "Failed to add stop_reason column (may already exist)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 24: Increase on-compact hook timeout from 5s to 30s in settings.json
    # The hook makes a synchronous Telegram HTTP call (urlopen) which was frequently
    # exceeding the 5-second process timeout, killing the hook before it could write
    # compaction-state.json. The missing file was the corroborating evidence.
    # Fix: patch the timeout field on the compact-matcher SessionStart hook entry.
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local compact_timeout
        compact_timeout=$(jq -r '
            [.hooks.SessionStart[]?
             | select(.matcher == "compact")
             | .hooks[]?.timeout // 0]
            | first // 0
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${compact_timeout}" = "5" ]; then
            TMP_SETTINGS=$(mktemp)
            jq '
                .hooks.SessionStart = [
                    .hooks.SessionStart[]?
                    | if .matcher == "compact" then
                        .hooks = [.hooks[]? | if .timeout == 5 then .timeout = 30 else . end]
                      else . end
                ]
            ' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Increased on-compact hook timeout from 5s to 30s (fixes Telegram call being killed)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 25: Remove periodic-self-check.sh cron entry
    # The self-check injected ~20 no-op inbox messages/hour that the dispatcher
    # immediately marked processed. Subagent results are delivered directly via
    # write_result; the periodic injection is pure noise with no functional value.
    local SELFCHECK_MARKER="# LOBSTER-SELF-CHECK"
    if crontab -l 2>/dev/null | grep -q "$SELFCHECK_MARKER"; then
        { crontab -l 2>/dev/null | grep -v "$SELFCHECK_MARKER" | grep -v "periodic-self-check" || true; } | crontab -
        substep "Removed periodic-self-check.sh cron entry (was generating ~20 no-op inbox entries/hour)"
        migrated=$((migrated + 1))
    fi

    # Migration 26: Register secret-scanner PreToolUse hook in Claude Code settings
    # New installs get this via install.sh; existing installs need this migration.
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local has_secret_scanner
        has_secret_scanner=$(jq -r '
            [.hooks.PreToolUse[]?.hooks[]?.command // empty]
            | map(select(contains("secret-scanner")))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${has_secret_scanner:-0}" = "0" ] || [ "${has_secret_scanner:-0}" = "" ]; then
            chmod +x "$LOBSTER_DIR/hooks/secret-scanner.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq --arg cmd "python3 $LOBSTER_DIR/hooks/secret-scanner.py" \
               '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
                "matcher": "mcp__lobster-inbox__send_reply|Bash",
                "hooks": [{
                    "type": "command",
                    "command": $cmd,
                    "timeout": 5
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered secret-scanner hook in Claude Code settings (warn mode)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 27: Add gws credential sync cron entry — superseded by Migration 34 (removed)

    # Migration 28: Add daily log-export cron entry
    # export-logs.py copies observations.log, lobster.log, and audit.jsonl to a
    # date-stamped archive under ~/lobster-workspace/logs/archive/ and writes a
    # summary to ~/messages/task-outputs/ (readable via check_task_outputs).
    # Provides an off-process durable copy of high-signal logs and a foundation
    # for future remote forwarding (see issue #730).
    local LOG_EXPORT_MARKER="# LOBSTER-LOG-EXPORT"
    local log_export_script="$LOBSTER_DIR/scheduled-tasks/export-logs.py"
    chmod +x "$log_export_script" 2>/dev/null || true
    # Remove any existing entry (stale path or schedule) then re-add with correct values
    crontab -l 2>/dev/null | grep -v "$LOG_EXPORT_MARKER" | crontab - 2>/dev/null || true
    (crontab -l 2>/dev/null; echo "0 3 * * * cd $LOBSTER_DIR && $HOME/.local/bin/uv run scheduled-tasks/export-logs.py $LOG_EXPORT_MARKER") | crontab -
    substep "Set daily log-export cron entry (03:00 UTC, archives observations.log + audit.jsonl)"
    migrated=$((migrated + 1))

    # Migration 29: Restore gws OAuth client secret from lobster-config — superseded by Migration 34 (removed)

    # Migration 30: Create ~/lobster-workspace/reports/ for artifact-based large result delivery.
    # Subagents write large outputs (reports, diffs, analysis) to this directory and pass the
    # path in write_result artifacts=[...]. The dispatcher reads and inlines the content rather
    # than bloating the inbox message or the dispatcher's context window (see issue #746).
    if [ ! -d "$WORKSPACE_DIR/reports" ]; then
        mkdir -p "$WORKSPACE_DIR/reports"
        substep "Created $WORKSPACE_DIR/reports/ for subagent artifact storage"
        migrated=$((migrated + 1))
    fi

    # Migration 31: Remove GitHub MCP server from Claude Code settings.
    # The GitHub MCP caused subagents to reach for mcp__github__* tools instead
    # of the gh CLI, which is already authenticated and the canonical tool.
    # Removing the MCP entry eliminates the confusion source at the tool-list level.
    # This migration removes the "github" MCP entry from both settings files so the
    # MCP no longer appears in the available tool list on next Claude Code startup.
    for _settings_file in "$HOME/.claude/settings.json" "$HOME/.claude/settings.local.json"; do
        if [ -f "$_settings_file" ] && jq -e '.mcpServers.github' "$_settings_file" >/dev/null 2>&1; then
            TMP_SETTINGS=$(mktemp)
            jq 'del(.mcpServers.github)' "$_settings_file" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$_settings_file"
            substep "Removed GitHub MCP entry from $_settings_file"
            migrated=$((migrated + 1))
        fi
    done
    # Also remove via claude CLI in case the MCP was registered at user scope
    if command -v claude &>/dev/null && claude mcp list 2>/dev/null | grep -q "^github"; then
        claude mcp remove github --scope user 2>/dev/null || true
        substep "Removed GitHub MCP server from Claude Code user config"
        migrated=$((migrated + 1))
    fi

    # Migration 32: Add LOBSTER_ENV=production to existing config.env files
    # New installs write LOBSTER_ENV=production into config.env during setup.
    # Existing installs that predate this change will not have the variable, which
    # is safe (both scripts default to "production" when the variable is absent),
    # but the explicit entry makes the knob discoverable and easy to flip for dev work.
    # We only append if LOBSTER_ENV is completely absent — no existing line is modified.
    if [ -f "$CONFIG_FILE" ] && ! grep -q '^LOBSTER_ENV=' "$CONFIG_FILE"; then
        cat >> "$CONFIG_FILE" << 'EOF'

# Environment mode: production | dev | test
# Set to "dev" to make the persistent session and health check inert while doing
# interactive SSH work. Revert to "production" (or remove this line) to resume.
LOBSTER_ENV=production
EOF
        substep "Added LOBSTER_ENV=production to $CONFIG_FILE (existing install backfill)"
        migrated=$((migrated + 1))
    fi

    # Migration 33: Register require-wait-for-messages Stop hook in settings.json
    # This hook fires on every Stop event and nudges the dispatcher to call
    # wait_for_messages when it stalls without doing so, cutting the recovery window
    # from ~12 minutes (health check) to one turn. Subagent sessions are exempted
    # via is_dispatcher() — the hook is a no-op for anything that is not the dispatcher.
    if [ -f "$CLAUDE_SETTINGS" ]; then
        if ! jq -e '.hooks.Stop[]? | select(.hooks[]?.command | contains("require-wait-for-messages"))' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
            chmod +x "$LOBSTER_DIR/hooks/require-wait-for-messages.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq '.hooks.Stop = (.hooks.Stop // []) + [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "python3 '"$LOBSTER_DIR"'/hooks/require-wait-for-messages.py",
                    "timeout": 10
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered require-wait-for-messages Stop hook in settings.json"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 34: Remove gws credential sync cron entry from existing installs.
    # gws (third-party Gmail CLI) is broken (OAuth 401 errors) and has been removed
    # from Lobster's install/setup. The daily cron entry it added must be cleaned
    # from existing installs so it no longer runs sync-gws-credentials.py.
    local GWS_SYNC_MARKER="# LOBSTER-GWS-CREDENTIAL-SYNC"
    if crontab -l 2>/dev/null | grep -q "$GWS_SYNC_MARKER"; then
        "$LOBSTER_DIR/scripts/cron-manage.sh" remove "$GWS_SYNC_MARKER" 2>/dev/null || true
        substep "Removed gws credential sync cron entry (gws integration discontinued)"
        migrated=$((migrated + 1))
    fi

    # Migration 35: Register on-fresh-start SessionStart hook in settings.json
    # On a fresh CC restart, all previously-"running" agent sessions are dead.
    # This hook runs agent-monitor.py --mark-failed immediately at startup so
    # stale sessions are cleared without waiting for the 120-minute reconciler
    # threshold. Skips compaction events and subagent sessions.
    if [ -f "$CLAUDE_SETTINGS" ]; then
        if ! jq -e '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-fresh-start"))' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
            chmod +x "$LOBSTER_DIR/hooks/on-fresh-start.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq '.hooks.SessionStart = (.hooks.SessionStart // []) + [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "python3 '"$LOBSTER_DIR"'/hooks/on-fresh-start.py",
                    "timeout": 30
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered on-fresh-start SessionStart hook in settings.json"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 36: Create sessions directory in lobster-user-config for numbered session notes
    # Session notes (YYYYMMDD-NNN.md) are the primary continuity mechanism for structured
    # memory. They live in lobster-user-config (committed, survives machine migrations).
    # Also seeds the session.template.md from canonical-templates if not already present.
    local sessions_dir="$USER_CONFIG_DIR/memory/canonical/sessions"
    if [ ! -d "$sessions_dir" ]; then
        mkdir -p "$sessions_dir"
        substep "Created $sessions_dir/ for numbered session note files"
        migrated=$((migrated + 1))
    fi
    local session_tmpl_src="$LOBSTER_DIR/memory/canonical-templates/sessions/session.template.md"
    local session_tmpl_dst="$sessions_dir/session.template.md"
    if [ -f "$session_tmpl_src" ] && [ ! -f "$session_tmpl_dst" ]; then
        cp "$session_tmpl_src" "$session_tmpl_dst"
        substep "Seeded session.template.md into $sessions_dir/"
        migrated=$((migrated + 1))
    fi

    # Migration 39: (removed) Previously copied bot-talk-poller.md and bot-talk-poller-fast.md
    # from scheduled-tasks/tasks/ into the workspace. Those files contained hardcoded instance
    # data (IP addresses, chat_ids, identity names) and have been removed from the public repo.
    # Instance-specific task files belong in ~/lobster-workspace/scheduled-jobs/tasks/ and are
    # created via MCP tools (create_scheduled_job) or user-config hooks — not pushed from the repo.

    # Migration 37: Remove run-job.sh cron entries and make dispatch-job.sh executable.
    # run-job.sh (which invoked claude -p directly) has been replaced by dispatch-job.sh
    # (which posts a scheduled_reminder to the inbox for the dispatcher to handle).
    # Remove any lingering LOBSTER-SCHEDULED cron entries that still reference run-job.sh.
    if crontab -l 2>/dev/null | grep -q 'run-job.sh.*# LOBSTER-SCHEDULED'; then
        { crontab -l 2>/dev/null | grep -v 'run-job.sh.*# LOBSTER-SCHEDULED' || true; } | crontab -
        substep "Removed run-job.sh cron entries (superseded by dispatch-job.sh inbox dispatch)"
        migrated=$((migrated + 1))
    fi
    # Make dispatch-job.sh executable if present
    local dispatch_script="$LOBSTER_DIR/scheduled-tasks/dispatch-job.sh"
    if [ -f "$dispatch_script" ] && [ ! -x "$dispatch_script" ]; then
        chmod +x "$dispatch_script"
        substep "Made dispatch-job.sh executable"
        migrated=$((migrated + 1))
    fi

    # Migration 40: Register block-claude-p.py PreToolUse hook in Claude Code settings
    # This hook detects and logs (warn mode) or blocks (block mode) `claude -p` /
    # `claude --print` invocations in Bash tool calls. Deploying in warn mode first
    # validates zero false positives before switching to hard-block. Mode is
    # controlled by LOBSTER_BLOCK_CLAUDE_P_MODE env var (default: warn).
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local has_block_claude_p
        has_block_claude_p=$(jq -r '
            [.hooks.PreToolUse[]?.hooks[]?.command // empty]
            | map(select(contains("block-claude-p")))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${has_block_claude_p:-0}" = "0" ] || [ "${has_block_claude_p:-0}" = "" ]; then
            chmod +x "$LOBSTER_DIR/hooks/block-claude-p.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq --arg cmd "python3 $LOBSTER_DIR/hooks/block-claude-p.py" \
               '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
                "matcher": "Bash",
                "hooks": [{
                    "type": "command",
                    "command": $cmd,
                    "timeout": 5
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered block-claude-p hook in Claude Code settings (warn mode, Bash-only)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 41: Replace bare python3 invocation in post-compact-gate PreToolUse hook with
    # a shell wrapper that skips Python startup when the sentinel file is absent.
    # On the 99%+ of tool calls where compact-pending does not exist, `test ! -f ...` exits
    # in ~1ms vs ~50ms for Python startup — eliminating ~14 unnecessary spawns per message cycle.
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local gate_cmd="python3 $LOBSTER_DIR/hooks/post-compact-gate.py"
        local gate_wrapper="test ! -f /home/lobster/messages/config/compact-pending || python3 $LOBSTER_DIR/hooks/post-compact-gate.py"
        local has_bare_gate
        has_bare_gate=$(jq -r --arg cmd "$gate_cmd" '
            [.hooks.PreToolUse[]?.hooks[]?.command // empty]
            | map(select(. == $cmd))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${has_bare_gate:-0}" != "0" ] && [ "${has_bare_gate:-0}" != "" ]; then
            TMP_SETTINGS=$(mktemp)
            jq --arg old "$gate_cmd" --arg new "$gate_wrapper" '
                .hooks.PreToolUse = [
                    .hooks.PreToolUse[]? |
                    .hooks = [
                        .hooks[]? |
                        if .command == $old then .command = $new else . end
                    ]
                ]
            ' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Updated post-compact-gate hook to use shell wrapper (skips Python when sentinel absent)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 42: Narrow context-monitor PostToolUse hook matcher from "" (every tool) to
    # "mcp__lobster-inbox__|Agent". Context window tracking is most relevant after MCP inbox
    # calls and Agent spawns — the two events where token consumption is highest. This reduces
    # PostToolUse spawns by ~65% with no meaningful loss of monitoring coverage.
    # Also registers the hook if it is absent entirely (for installs that predate install.sh entry).
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local has_monitor_any
        has_monitor_any=$(jq -r '
            [.hooks.PostToolUse[]?.hooks[]?.command // empty]
            | map(select(contains("context-monitor")))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${has_monitor_any:-0}" = "0" ] || [ "${has_monitor_any:-0}" = "" ]; then
            # Hook is absent — install it with the correct (narrow) matcher.
            chmod +x "$LOBSTER_DIR/hooks/context-monitor.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq --arg cmd "python3 $LOBSTER_DIR/hooks/context-monitor.py" \
               '.hooks.PostToolUse = (.hooks.PostToolUse // []) + [{
                "matcher": "mcp__lobster-inbox__|Agent",
                "hooks": [{
                    "type": "command",
                    "command": $cmd,
                    "timeout": 5
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered context-monitor hook with narrow matcher (mcp__lobster-inbox__|Agent)"
            migrated=$((migrated + 1))
        else
            # Hook exists — check if it has the old empty matcher and fix it.
            local has_empty_matcher
            has_empty_matcher=$(jq -r '
                [.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("context-monitor")) | .matcher]
                | map(select(. == ""))
                | length
            ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
            if [ "${has_empty_matcher:-0}" != "0" ] && [ "${has_empty_matcher:-0}" != "" ]; then
                TMP_SETTINGS=$(mktemp)
                jq '
                    .hooks.PostToolUse = [
                        .hooks.PostToolUse[]? |
                        if (.hooks[]?.command | contains("context-monitor")) and .matcher == ""
                        then .matcher = "mcp__lobster-inbox__|Agent"
                        else .
                        end
                    ]
                ' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
                substep "Narrowed context-monitor matcher from empty to mcp__lobster-inbox__|Agent"
                migrated=$((migrated + 1))
            fi
        fi
    fi

    # Migration 43: Switch MCP transport from stdio to HTTP (issue #960).
    # The lobster-mcp-local systemd service now runs inbox_server.py as a
    # persistent HTTP server on localhost:8766.  Claude Code must be registered
    # to connect via "url" instead of a stdio command so that CC auto-updates
    # no longer kill the MCP server (they would close the stdio pipe).
    #
    # This migration:
    #   a) Installs (or updates) the lobster-mcp-local systemd service.
    #   b) Re-registers the lobster-inbox MCP server using HTTP transport.
    #
    # Idempotent: skipped if the HTTP registration already exists.
    local mcp_http_already_registered
    mcp_http_already_registered=$(claude mcp list 2>/dev/null | grep -c "localhost:8766" || echo "0")
    if [ "${mcp_http_already_registered:-0}" = "0" ]; then
        # Install / refresh the lobster-mcp-local service
        local mcp_local_template="$LOBSTER_DIR/services/lobster-mcp-local.service.template"
        local mcp_local_service="$LOBSTER_DIR/services/lobster-mcp-local.service"

        if [ -f "$mcp_local_template" ]; then
            # Use the shared template library when available (it is, since we
            # run from an existing install with the repo already cloned).
            # Falls back to inline sed only if the lib file is somehow missing.
            local _lib="${LOBSTER_DIR}/scripts/lib/template.sh"
            if [ -f "$_lib" ]; then
                # Set canonical LOBSTER_* vars the library expects
                LOBSTER_USER="${LOBSTER_USER:-$(whoami)}"
                LOBSTER_GROUP="${LOBSTER_GROUP:-$(id -gn)}"
                LOBSTER_HOME="${LOBSTER_HOME:-$HOME}"
                LOBSTER_INSTALL_DIR="$LOBSTER_DIR"
                LOBSTER_WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
                LOBSTER_MESSAGES="${LOBSTER_MESSAGES:-$HOME/messages}"
                LOBSTER_CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
                LOBSTER_USER_CONFIG="${LOBSTER_USER_CONFIG:-$HOME/lobster-user-config}"
                # shellcheck source=lib/template.sh
                source "$_lib"
                _tmpl_generate_from_template "$mcp_local_template" "$mcp_local_service"
            else
                # Fallback: inline rendering (all 8 placeholders — keep in sync with lib)
                local _user _group _home _config_dir _messages_dir _workspace_dir _user_config_dir
                _user=$(whoami)
                _group=$(id -gn)
                _home="$HOME"
                _config_dir="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
                _messages_dir="${LOBSTER_MESSAGES:-$HOME/messages}"
                _workspace_dir="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
                _user_config_dir="${LOBSTER_USER_CONFIG:-$HOME/lobster-user-config}"
                sed \
                    -e "s|{{USER}}|$_user|g" \
                    -e "s|{{GROUP}}|$_group|g" \
                    -e "s|{{HOME}}|$_home|g" \
                    -e "s|{{INSTALL_DIR}}|$LOBSTER_DIR|g" \
                    -e "s|{{CONFIG_DIR}}|$_config_dir|g" \
                    -e "s|{{MESSAGES_DIR}}|$_messages_dir|g" \
                    -e "s|{{WORKSPACE_DIR}}|$_workspace_dir|g" \
                    -e "s|{{USER_CONFIG_DIR}}|$_user_config_dir|g" \
                    "$mcp_local_template" > "$mcp_local_service"
            fi
        fi

        if [ -f "$mcp_local_service" ] && pidof systemd >/dev/null 2>&1; then
            sudo cp "$mcp_local_service" /etc/systemd/system/
            sudo systemctl daemon-reload
            sudo systemctl enable lobster-mcp-local 2>/dev/null || true
            sudo systemctl restart lobster-mcp-local 2>/dev/null || true
            substep "lobster-mcp-local service installed and (re)started"
            # Wait briefly for the server to come up before re-registering
            sleep 3
        fi

        # Remove any legacy mcpServers.lobster-inbox entry from settings.json if present.
        # The claude mcp CLI stores entries in ~/.claude.json, not settings.json,
        # but defensive cleanup costs nothing and handles any manual or legacy configs.
        if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
            if jq -e '.mcpServers."lobster-inbox"' "$CLAUDE_SETTINGS" >/dev/null 2>&1; then
                TMP_SETTINGS=$(mktemp)
                jq 'del(.mcpServers."lobster-inbox")' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
                substep "Removed legacy mcpServers.lobster-inbox entry from settings.json"
            fi
        fi

        # Re-register MCP server using HTTP transport
        claude mcp remove lobster-inbox 2>/dev/null || true
        if claude mcp add --transport http lobster-inbox -s user "http://localhost:8766/mcp" 2>/dev/null; then
            substep "lobster-inbox re-registered with HTTP transport (http://localhost:8766/mcp)"
            migrated=$((migrated + 1))
        else
            warn "Migration 43: MCP HTTP re-registration may have failed. Run: claude mcp list"
        fi
    fi

    # Migration 44: Switch bot-talk-poller cron entry to use bot-talk-check-dispatch.sh.
    # The pre-check wrapper queries the bot-talk API before writing to the inbox,
    # so no LLM subagent is spawned on empty polls. The runner field in jobs.json
    # drives this via sync-crontab.sh; this migration re-syncs the crontab so the
    # change takes effect on existing installs without a manual sync.
    local BOT_TALK_CHECK_SCRIPT="$LOBSTER_DIR/scheduled-tasks/bot-talk-check-dispatch.sh"
    if [ -f "$BOT_TALK_CHECK_SCRIPT" ]; then
        if ! crontab -l 2>/dev/null | grep -q "bot-talk-check-dispatch.sh"; then
            chmod +x "$BOT_TALK_CHECK_SCRIPT" 2>/dev/null || true
            # Re-run sync-crontab.sh to rebuild the crontab from jobs.json, picking up
            # the new runner field for bot-talk-poller.
            if [ -f "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" ]; then
                chmod +x "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                "$LOBSTER_DIR/scheduled-tasks/sync-crontab.sh" 2>/dev/null || true
                substep "Crontab re-synced: bot-talk-poller now uses bot-talk-check-dispatch.sh"
                migrated=$((migrated + 1))
            fi
        fi
    fi

    # Migration 46: Add lobster user to the `crontab` group.
    # The MCP server process runs under PR_SET_NO_NEW_PRIVS (NoNewPrivs=1), which
    # suppresses setgid bits on child processes. The `crontab` binary is setgid-crontab,
    # so `crontab -` fails with "mkstemp: Permission denied" when called from the MCP
    # server. Fix: add the lobster user to the crontab group so sync-crontab.sh can
    # write directly to /var/spool/cron/crontabs/$USER (group-writable directory) without
    # needing the setgid bit. Requires sudo; warns and skips if sudo is unavailable.
    local CRONTAB_DIR="/var/spool/cron/crontabs"
    if [ -d "$CRONTAB_DIR" ] && ! id -nG "$USER" | grep -qw "crontab"; then
        if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
            sudo usermod -aG crontab "$USER" 2>/dev/null && {
                substep "Added $USER to the crontab group (fixes NoNewPrivs crontab permission error)"
                migrated=$((migrated + 1))
                warn "Group membership change takes effect at next login. Run 'newgrp crontab' or restart the Lobster service to apply immediately."
            } || warn "Failed to add $USER to crontab group — run: sudo usermod -aG crontab $USER"
        else
            warn "Cannot add $USER to crontab group (sudo unavailable). Run manually: sudo usermod -aG crontab $USER"
            warn "Until this is done, create_scheduled_job/update_scheduled_job/delete_scheduled_job will fail to sync crontab."
        fi
    fi


    # Migration 47: Seed ifttt-rules.yaml in lobster-user-config/memory/canonical/
    # Introduces the IFTTT-style behavioral rules store (issue #853). The file is
    # machine-readable YAML, bounded to 100 rules, and managed autonomously by Lobster.
    # Existing installs that predate this change need the file seeded so the dispatcher
    # can load rules at startup without errors. The file starts empty (rules: []) so
    # no behavioral change occurs on upgrade — rules accumulate over time.
    local ifttt_src="$LOBSTER_DIR/memory/canonical-templates/ifttt-rules.yaml"
    local ifttt_dst="$USER_CONFIG_DIR/memory/canonical/ifttt-rules.yaml"
    if [ -f "$ifttt_src" ] && [ ! -f "$ifttt_dst" ]; then
        cp "$ifttt_src" "$ifttt_dst"
        substep "Seeded ifttt-rules.yaml into $USER_CONFIG_DIR/memory/canonical/"
        migrated=$((migrated + 1))
    fi

    # Migration 48: Add idempotency column to agent_sessions.
    # The idempotency column enables safe orphan recovery after restarts (#866).
    # Sessions classified as 'safe' can be re-run automatically; 'unsafe'/'unknown'
    # sessions surface a user notification instead. The column is also used by the
    # session_start and register_agent MCP tools so the dispatcher can classify
    # tasks at spawn time. Migration is a no-op on fresh installs (column already
    # in CREATE TABLE DDL). On existing installs it adds the column with DEFAULT 'unknown'.
    # The Python session_store migration list also handles this idempotently — this
    # upgrade.sh entry is the documentation anchor and ensures crontab/service
    # restarts don't miss the schema change on minimal installs without uv.
    if command -v uv &>/dev/null; then
        uv run python -c "
import sqlite3, os
db_path = os.path.expanduser('~/messages/config/agent_sessions.db')
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(\"ALTER TABLE agent_sessions ADD COLUMN idempotency TEXT DEFAULT 'unknown'\")
        conn.commit()
        print('idempotency column added')
    except sqlite3.OperationalError:
        print('idempotency column already exists')
    finally:
        conn.close()
else:
    print('agent_sessions.db not found — will be created on next server start')
" 2>/dev/null && substep "agent_sessions.idempotency column present (fresh or migrated)" && migrated=$((migrated + 1)) || true
    fi

    # Migration 52: Add LOBSTER-GHOST-DETECTOR cron entry.
    # agent-monitor.py runs every 30 minutes and calls --alert --mark-failed directly,
    # sending Telegram alerts when ghost agents are found. No LLM subagent is needed.
    # Previously this was routed through REMINDER_ROUTING in sys.dispatcher.bootup.md
    # which spawned a lobster-generalist just to run the script and relay its output.
    # That LLM relay layer has been removed; the script now runs directly from cron.
    local GHOST_DETECTOR_MARKER="# LOBSTER-GHOST-DETECTOR"
    # Remove any existing entry (stale path or schedule) then re-add with correct values
    crontab -l 2>/dev/null | grep -v "$GHOST_DETECTOR_MARKER" | crontab - 2>/dev/null || true
    (crontab -l 2>/dev/null; echo "*/30 * * * * cd $HOME && $HOME/.local/bin/uv run $LOBSTER_DIR/scripts/agent-monitor.py --alert --mark-failed >> $WORKSPACE_DIR/logs/agent-monitor.log 2>&1 $GHOST_DETECTOR_MARKER") | crontab -
    substep "Set ghost detector cron entry (agent-monitor.py --alert --mark-failed, every 30 min)"
    migrated=$((migrated + 1))

    # Migration 53: Add LOBSTER-OOM-CHECK cron entry.
    # oom-monitor.py runs every 10 minutes, scans the kernel journal for OOM kills,
    # and writes inbox messages directly when new events are detected. No LLM needed.
    # Previously this was routed through REMINDER_ROUTING which spawned a subagent.
    # Only active when LOBSTER_DEBUG=true (the script exits 0 silently otherwise).
    local OOM_CHECK_MARKER="# LOBSTER-OOM-CHECK"
    if ! crontab -l 2>/dev/null | grep -q "$OOM_CHECK_MARKER"; then
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$OOM_CHECK_MARKER" \
            "*/10 * * * * cd $HOME && uv run $LOBSTER_DIR/scripts/oom-monitor.py --since-minutes 10 >> $WORKSPACE_DIR/logs/oom-monitor.log 2>&1 $OOM_CHECK_MARKER"
        substep "Added OOM monitor cron entry (oom-monitor.py --since-minutes 10, every 10 min)"
        migrated=$((migrated + 1))
    fi

    # Migration 54: Add inbox-staleness-warn.sh cron entry
    # Injects a scheduled_reminder into the inbox when the oldest unprocessed
    # user message has been waiting for 3+ minutes. Gives the dispatcher an
    # in-band nudge to call wait_for_messages or delegate, complementing the
    # health-check restart path (which only fires at 8+ minutes). Dedup prevents
    # multiple warnings per staleness event.
    local STALENESS_WARN_MARKER="# LOBSTER-INBOX-STALENESS-WARN"
    if ! crontab -l 2>/dev/null | grep -q "$STALENESS_WARN_MARKER"; then
        chmod +x "$LOBSTER_DIR/scripts/inbox-staleness-warn.sh" 2>/dev/null || true
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$STALENESS_WARN_MARKER" \
            "*/1 * * * * $LOBSTER_DIR/scripts/inbox-staleness-warn.sh $STALENESS_WARN_MARKER"
        substep "Added inbox-staleness-warn.sh cron entry (runs every minute, warns at 3-minute staleness)"
        migrated=$((migrated + 1))
    fi

    # Migration 55: Migrate existing jobs.json entries to systemd timers.
    # The cron+jobs.json scheduling backend has been replaced by systemd timers
    # (see PR #1105). Existing entries in ~/lobster-workspace/scheduled-jobs/jobs.json
    # will no longer fire because sync-crontab.sh is no longer called.
    #
    # Sudoers note: install.sh already grants `lobster ALL=(ALL) NOPASSWD:ALL`,
    # which covers `sudo systemctl` and `sudo tee`. No separate sudoers entry is needed.
    #
    # This migration reads jobs.json and, for each enabled job that has a
    # `command` field set, creates the corresponding systemd timer unit.
    # Jobs without a command field cannot be migrated automatically — they
    # will be reported as warnings so the user can recreate them via
    # create_scheduled_job with an explicit command.
    local JOBS_JSON="$WORKSPACE_DIR/scheduled-jobs/jobs.json"
    if [ -f "$JOBS_JSON" ] && command -v python3 >/dev/null 2>&1 && pidof systemd >/dev/null 2>&1; then
        local jobs_count
        jobs_count=$(python3 -c "
import json, sys
try:
    d = json.loads(open('$JOBS_JSON').read())
    print(len(d.get('jobs', {})))
except Exception:
    print(0)
" 2>/dev/null || echo "0")
        if [ "${jobs_count:-0}" -gt 0 ]; then
            substep "Migrating $jobs_count jobs.json entries to systemd timers..."
            JOBS_JSON_PATH="$JOBS_JSON"
            python3 - "$JOBS_JSON_PATH" <<'PYEOF'
import json, subprocess, sys
from pathlib import Path

JOBS_FILE = Path(sys.argv[1])
SYSTEMD_DIR = Path("/etc/systemd/system")
UNIT_PREFIX = "lobster-"
LOBSTER_MARKER = "# LOBSTER-MANAGED"
LOBSTER_USER = "lobster"

def sudo_write(path: Path, content: str) -> None:
    """Write content to a root-owned path using sudo tee."""
    result = subprocess.run(
        ["sudo", "tee", str(path)],
        input=content.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        raise PermissionError(f"sudo tee {path} failed: {result.stderr.decode().strip()}")

try:
    data = json.loads(JOBS_FILE.read_text())
except Exception as e:
    print(f"  warning: could not read jobs.json: {e}", file=sys.stderr)
    sys.exit(0)

jobs = data.get("jobs", {})
migrated = 0
skipped = []

for name, job in jobs.items():
    if not job.get("enabled", True):
        continue

    command = job.get("command") or job.get("runner") or ""
    schedule = job.get("schedule", "")

    if not command or not command.startswith("/"):
        skipped.append((name, "no absolute command path — recreate with create_scheduled_job"))
        continue

    if not schedule:
        skipped.append((name, "no schedule — recreate with create_scheduled_job"))
        continue

    timer_path = SYSTEMD_DIR / f"{UNIT_PREFIX}{name}.timer"
    service_path = SYSTEMD_DIR / f"{UNIT_PREFIX}{name}.service"

    # Skip if already migrated
    try:
        if timer_path.exists() and LOBSTER_MARKER in timer_path.read_text():
            continue
    except OSError:
        pass

    desc = job.get("description") or f"Lobster scheduled job: {name}"

    timer_content = f"""[Unit]
Description={desc}
{LOBSTER_MARKER}

[Timer]
OnCalendar={schedule}
Persistent=true

[Install]
WantedBy=timers.target
"""
    service_content = f"""[Unit]
Description={desc}
{LOBSTER_MARKER}

[Service]
Type=oneshot
User={LOBSTER_USER}
ExecStart={command}
"""

    try:
        sudo_write(timer_path, timer_content)
        sudo_write(service_path, service_content)
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["sudo", "systemctl", "enable", "--now", f"{UNIT_PREFIX}{name}.timer"],
                       check=True, capture_output=True)
        print(f"  migrated: {name} ({schedule}) -> {UNIT_PREFIX}{name}.timer")
        migrated += 1
    except Exception as e:
        skipped.append((name, f"error: {e}"))

print(f"  {migrated} job(s) migrated to systemd timers")
for sname, reason in skipped:
    print(f"  WARN: '{sname}' skipped — {reason}", file=sys.stderr)
PYEOF
        fi
    fi

    # Migration 60: Register inject-bootup-context.py SessionStart hooks in settings.json
    # Adds two SessionStart entries: one empty-matcher entry for all fresh sessions
    # (must run after write-dispatcher-session-id so role detection works), and one
    # compact-matcher entry so bootup content is re-injected after context compaction.
    if [ -f "$CLAUDE_SETTINGS" ]; then
        chmod +x "$LOBSTER_DIR/hooks/inject-bootup-context.py" 2>/dev/null || true
        if ! jq -e '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
            TMP_SETTINGS=$(mktemp)
            jq '.hooks.SessionStart = (.hooks.SessionStart // []) + [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "python3 '"$LOBSTER_DIR"'/hooks/inject-bootup-context.py",
                    "timeout": 10
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered inject-bootup-context SessionStart hook (all sessions)"
            migrated=$((migrated + 1))
        fi
        if ! jq -e '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "compact")' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
            TMP_SETTINGS=$(mktemp)
            jq '.hooks.SessionStart = (.hooks.SessionStart // []) + [{
                "matcher": "compact",
                "hooks": [{
                    "type": "command",
                    "command": "python3 '"$LOBSTER_DIR"'/hooks/inject-bootup-context.py",
                    "timeout": 10
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered inject-bootup-context SessionStart hook (compact sessions)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 61: Add nightly-consolidation crontab entry.
    # install.sh adds this entry but existing installs may be missing it.
    # Runs at 3am daily to consolidate memory and rotate digests.
    local NIGHTLY_CONSOLIDATION_MARKER="# LOBSTER-NIGHTLY-CONSOLIDATION"
    if ! crontab -l 2>/dev/null | grep -q "$NIGHTLY_CONSOLIDATION_MARKER"; then
        chmod +x "$LOBSTER_DIR/scripts/nightly-consolidation.sh" 2>/dev/null || true
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$NIGHTLY_CONSOLIDATION_MARKER" \
            "0 3 * * * $LOBSTER_DIR/scripts/nightly-consolidation.sh $NIGHTLY_CONSOLIDATION_MARKER"
        substep "nightly-consolidation crontab entry added (runs at 3am daily)"
        migrated=$((migrated + 1))
    else
        substep "nightly-consolidation crontab entry already present"

    fi

    # Migration 61: Add nightly-consolidation crontab entry.
    # install.sh adds this entry but existing installs may be missing it.
    # Runs at 3am daily to consolidate memory and rotate digests.
    local NIGHTLY_CONSOLIDATION_MARKER="# LOBSTER-NIGHTLY-CONSOLIDATION"
    if ! crontab -l 2>/dev/null | grep -q "$NIGHTLY_CONSOLIDATION_MARKER"; then
        chmod +x "$LOBSTER_DIR/scripts/nightly-consolidation.sh" 2>/dev/null || true
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$NIGHTLY_CONSOLIDATION_MARKER" \
            "0 3 * * * $LOBSTER_DIR/scripts/nightly-consolidation.sh $NIGHTLY_CONSOLIDATION_MARKER"
        substep "nightly-consolidation crontab entry added (runs at 3am daily)"
        migrated=$((migrated + 1))
    else
        substep "nightly-consolidation crontab entry already present"

    fi

    # Migration 56: Add LOBSTER_ADMIN_CHAT_ID to config.env if missing.
    # alert.sh and the transcription worker use this to send error notifications
    # directly to the admin. Without it, alerts are silently dropped.
    # Defaults to the first entry in TELEGRAM_ALLOWED_USERS (which is the owner).
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE" 2>/dev/null || true
        if [ -z "${LOBSTER_ADMIN_CHAT_ID:-}" ]; then
            # Derive from TELEGRAM_ALLOWED_USERS — first comma-separated value
            local first_allowed
            first_allowed=$(echo "${TELEGRAM_ALLOWED_USERS:-}" | cut -d',' -f1 | tr -d '[:space:]')
            if [ -n "$first_allowed" ]; then
                echo "" >> "$CONFIG_FILE"
                echo "# Admin chat ID for system alerts (auto-derived from TELEGRAM_ALLOWED_USERS)" >> "$CONFIG_FILE"
                echo "LOBSTER_ADMIN_CHAT_ID=$first_allowed" >> "$CONFIG_FILE"
                substep "Added LOBSTER_ADMIN_CHAT_ID=$first_allowed to config.env"
                migrated=$((migrated + 1))
            else
                warn "LOBSTER_ADMIN_CHAT_ID missing and could not be derived — set it manually in $CONFIG_FILE"
            fi
        fi
    fi

    # Migration 57: Add LOBSTER_INTERNAL_SECRET to config.env if missing.
    # Required for the push-calendar-token endpoint in inbox_server_http.py.
    # Without it, Google Calendar token pushes from the remote bridge are disabled.
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE" 2>/dev/null || true
        if [ -z "${LOBSTER_INTERNAL_SECRET:-}" ]; then
            local generated_secret
            generated_secret=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || \
                               openssl rand -hex 32 2>/dev/null || \
                               echo "")
            if [ -n "$generated_secret" ]; then
                echo "" >> "$CONFIG_FILE"
                echo "# Internal secret for authenticated MCP HTTP endpoints (e.g. push-calendar-token)" >> "$CONFIG_FILE"
                echo "LOBSTER_INTERNAL_SECRET=$generated_secret" >> "$CONFIG_FILE"
                substep "Generated and added LOBSTER_INTERNAL_SECRET to config.env"
                migrated=$((migrated + 1))
            else
                warn "LOBSTER_INTERNAL_SECRET missing and could not be generated — set it manually in $CONFIG_FILE"
            fi
        fi
    fi

    # Migration 58: Add LOBSTER-DAILY-HEALTH cron entry.
    # install.sh registers daily-health-check.sh at 06:00 UTC; existing installs
    # that were set up before this cron was added will not have it.
    local DAILY_HEALTH_SCRIPT="$LOBSTER_DIR/scripts/daily-health-check.sh"
    if [ -f "$DAILY_HEALTH_SCRIPT" ]; then
        if ! crontab -l 2>/dev/null | grep -q "LOBSTER-DAILY-HEALTH"; then
            chmod +x "$DAILY_HEALTH_SCRIPT" 2>/dev/null || true
            "$LOBSTER_DIR/scripts/cron-manage.sh" add "# LOBSTER-DAILY-HEALTH" \
                "0 6 * * * $DAILY_HEALTH_SCRIPT # LOBSTER-DAILY-HEALTH" 2>/dev/null && {
                substep "Added LOBSTER-DAILY-HEALTH cron entry (daily-health-check.sh, 06:00 UTC)"
                migrated=$((migrated + 1))
            } || warn "Could not add LOBSTER-DAILY-HEALTH cron entry — check cron-manage.sh"
        fi
    fi

    # Migration 59: Seed obsidian.env from template if missing.
    # The obsidian-km skill requires ~/lobster-config/obsidian.env to exist.
    # On existing installs the file may not be present; seed it from the template
    # so the skill can be activated without manual setup steps.
    local OBSIDIAN_ENV="$LOBSTER_CONFIG_DIR/obsidian.env"
    local OBSIDIAN_TEMPLATE="$LOBSTER_DIR/lobster-shop/obsidian-km/config/obsidian.env.template"
    if [ -f "$OBSIDIAN_TEMPLATE" ] && [ ! -f "$OBSIDIAN_ENV" ]; then
        cp "$OBSIDIAN_TEMPLATE" "$OBSIDIAN_ENV"
        substep "Seeded $OBSIDIAN_ENV from template (configure OBSIDIAN_VAULT_PATH before use)"
        migrated=$((migrated + 1))
    fi

    # Migration 56: Add LOBSTER_ADMIN_CHAT_ID to config.env if missing.
    # alert.sh and the transcription worker use this to send error notifications
    # directly to the admin. Without it, alerts are silently dropped.
    # Defaults to the first entry in TELEGRAM_ALLOWED_USERS (which is the owner).
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE" 2>/dev/null || true
        if [ -z "${LOBSTER_ADMIN_CHAT_ID:-}" ]; then
            # Derive from TELEGRAM_ALLOWED_USERS — first comma-separated value
            local first_allowed
            first_allowed=$(echo "${TELEGRAM_ALLOWED_USERS:-}" | cut -d',' -f1 | tr -d '[:space:]')
            if [ -n "$first_allowed" ]; then
                echo "" >> "$CONFIG_FILE"
                echo "# Admin chat ID for system alerts (auto-derived from TELEGRAM_ALLOWED_USERS)" >> "$CONFIG_FILE"
                echo "LOBSTER_ADMIN_CHAT_ID=$first_allowed" >> "$CONFIG_FILE"
                substep "Added LOBSTER_ADMIN_CHAT_ID=$first_allowed to config.env"
                migrated=$((migrated + 1))
            else
                warn "LOBSTER_ADMIN_CHAT_ID missing and could not be derived — set it manually in $CONFIG_FILE"
            fi
        fi
    fi

    # Migration 57: Add LOBSTER_INTERNAL_SECRET to config.env if missing.
    # Required for the push-calendar-token endpoint in inbox_server_http.py.
    # Without it, Google Calendar token pushes from the remote bridge are disabled.
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE" 2>/dev/null || true
        if [ -z "${LOBSTER_INTERNAL_SECRET:-}" ]; then
            local generated_secret
            generated_secret=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || \
                               openssl rand -hex 32 2>/dev/null || \
                               echo "")
            if [ -n "$generated_secret" ]; then
                echo "" >> "$CONFIG_FILE"
                echo "# Internal secret for authenticated MCP HTTP endpoints (e.g. push-calendar-token)" >> "$CONFIG_FILE"
                echo "LOBSTER_INTERNAL_SECRET=$generated_secret" >> "$CONFIG_FILE"
                substep "Generated and added LOBSTER_INTERNAL_SECRET to config.env"
                migrated=$((migrated + 1))
            else
                warn "LOBSTER_INTERNAL_SECRET missing and could not be generated — set it manually in $CONFIG_FILE"
            fi
        fi
    fi

    # Migration 58: Add LOBSTER-DAILY-HEALTH cron entry.
    # install.sh registers daily-health-check.sh at 06:00 UTC; existing installs
    # that were set up before this cron was added will not have it.
    local DAILY_HEALTH_SCRIPT="$LOBSTER_DIR/scripts/daily-health-check.sh"
    if [ -f "$DAILY_HEALTH_SCRIPT" ]; then
        if ! crontab -l 2>/dev/null | grep -q "LOBSTER-DAILY-HEALTH"; then
            chmod +x "$DAILY_HEALTH_SCRIPT" 2>/dev/null || true
            "$LOBSTER_DIR/scripts/cron-manage.sh" add "# LOBSTER-DAILY-HEALTH" \
                "0 6 * * * $DAILY_HEALTH_SCRIPT # LOBSTER-DAILY-HEALTH" 2>/dev/null && {
                substep "Added LOBSTER-DAILY-HEALTH cron entry (daily-health-check.sh, 06:00 UTC)"
                migrated=$((migrated + 1))
            } || warn "Could not add LOBSTER-DAILY-HEALTH cron entry — check cron-manage.sh"
        fi
    fi

    # Migration 59: Seed obsidian.env from template if missing.
    # The obsidian-km skill requires ~/lobster-config/obsidian.env to exist.
    # On existing installs the file may not be present; seed it from the template
    # so the skill can be activated without manual setup steps.
    local OBSIDIAN_ENV="$LOBSTER_CONFIG_DIR/obsidian.env"
    local OBSIDIAN_TEMPLATE="$LOBSTER_DIR/lobster-shop/obsidian-km/config/obsidian.env.template"
    if [ -f "$OBSIDIAN_TEMPLATE" ] && [ ! -f "$OBSIDIAN_ENV" ]; then
        cp "$OBSIDIAN_TEMPLATE" "$OBSIDIAN_ENV"
        substep "Seeded $OBSIDIAN_ENV from template (configure OBSIDIAN_VAULT_PATH before use)"
        migrated=$((migrated + 1))
    fi

    # Migration 62: Ensure ~/messages/config/group-whitelist.json exists.
    # The group chat gating system (Phases 1-4) reads this file at startup.
    # On existing installs the config/ subdirectory may not exist; this creates
    # it and seeds an empty whitelist so the bot starts cleanly without errors.
    local MESSAGES_CONFIG_DIR="$HOME/messages/config"
    if [ ! -d "$MESSAGES_CONFIG_DIR" ]; then
        mkdir -p "$MESSAGES_CONFIG_DIR"
        substep "Created $MESSAGES_CONFIG_DIR"
        migrated=$((migrated + 1))
    fi
    if [ ! -f "$MESSAGES_CONFIG_DIR/group-whitelist.json" ]; then
        echo '{"groups": {}}' > "$MESSAGES_CONFIG_DIR/group-whitelist.json"
        substep "Created empty $MESSAGES_CONFIG_DIR/group-whitelist.json"
        migrated=$((migrated + 1))
    fi

    # Migration 63: Rename data/events.jsonl -> data/memory-events.jsonl.
    # StaticMemory now writes to memory-events.jsonl to distinguish it from the
    # EventBus operational log at logs/events.jsonl. Rename any existing file
    # so history is preserved without manual intervention.
    if [ -f "$WORKSPACE_DIR/data/events.jsonl" ] && [ ! -f "$WORKSPACE_DIR/data/memory-events.jsonl" ]; then
        mv "$WORKSPACE_DIR/data/events.jsonl" "$WORKSPACE_DIR/data/memory-events.jsonl"
        substep "Renamed data/events.jsonl -> data/memory-events.jsonl"
        migrated=$((migrated + 1))
    fi

    # Migration 64: Add message_claims and dispatcher_lock tables to agent_sessions.db
    # These tables are the SQLite-backed claim gate introduced in issue #1360.
    # message_claims: UNIQUE PRIMARY KEY on message_id — INSERT OR FAIL provides
    #   exclusive ownership without filesystem rename races.
    # dispatcher_lock: single-row table (CHECK id=1) — enforces at most one active
    #   dispatcher loop at any time.
    local AGENT_SESSIONS_DB="${LOBSTER_MESSAGES:-$HOME/messages}/config/agent_sessions.db"
    if [ -f "$AGENT_SESSIONS_DB" ]; then
        if ! sqlite3 "$AGENT_SESSIONS_DB" "PRAGMA table_info(message_claims);" 2>/dev/null | grep -q "message_id"; then
            substep "Adding message_claims table to agent_sessions.db..."
            sqlite3 "$AGENT_SESSIONS_DB" "
CREATE TABLE IF NOT EXISTS message_claims (
    message_id  TEXT PRIMARY KEY,
    claimed_by  TEXT NOT NULL,
    claimed_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'processing'
);" 2>/dev/null && \
                success "message_claims table created" || \
                warn "Failed to create message_claims table (may already exist)"
            migrated=$((migrated + 1))
        fi
        if ! sqlite3 "$AGENT_SESSIONS_DB" "PRAGMA table_info(dispatcher_lock);" 2>/dev/null | grep -q "session_id"; then
            substep "Adding dispatcher_lock table to agent_sessions.db..."
            sqlite3 "$AGENT_SESSIONS_DB" "
CREATE TABLE IF NOT EXISTS dispatcher_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    session_id  TEXT NOT NULL,
    locked_at   TEXT NOT NULL
);" 2>/dev/null && \
                success "dispatcher_lock table created" || \
                warn "Failed to create dispatcher_lock table (may already exist)"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 65: Re-deploy all plain task file templates to runtime directory to fix
    # template drift (issue #1404). When a PR updates a task file in scheduled-tasks/tasks/,
    # the change was not propagated to already-deployed runtime copies in
    # $WORKSPACE_DIR/scheduled-jobs/tasks/. This migration overwrites every plain .md file
    # (not .md.template — those require placeholder substitution) so existing installs
    # stay in sync with the repo without a full reinstall.
    local repo_tasks_dir="$LOBSTER_DIR/scheduled-tasks/tasks"
    local runtime_tasks_dir="$WORKSPACE_DIR/scheduled-jobs/tasks"
    if [ -d "$repo_tasks_dir" ]; then
        mkdir -p "$runtime_tasks_dir"
        for task_file in "$repo_tasks_dir"/*.md; do
            [ -f "$task_file" ] || continue
            local base
            base=$(basename "$task_file")
            [ "$base" = "README.md" ] && continue
            cp "$task_file" "$runtime_tasks_dir/$base"
            substep "Re-deployed task template: $base"
            migrated=$((migrated + 1))
        done
    fi

    # Migration 66: Install PostToolUse thinking-heartbeat hook (issue #1401).
    # The hook writes last_thinking_at to lobster-state.json on every tool call,
    # giving the health check a freshness signal during the dispatcher's reasoning
    # phase (10+ minutes of LLM work with no WFM or mark_processed calls).
    chmod +x "$LOBSTER_DIR/hooks/thinking-heartbeat.py" 2>/dev/null || true
    if [ -f "$CLAUDE_SETTINGS" ]; then
        if ! jq -e '.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("thinking-heartbeat"))' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
            TMP_SETTINGS=$(mktemp)
            jq '.hooks.PostToolUse = (.hooks.PostToolUse // []) + [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "python3 '"$LOBSTER_DIR"'/hooks/thinking-heartbeat.py",
                    "timeout": 5
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Installed thinking-heartbeat PostToolUse hook"
            migrated=$((migrated + 1))
        fi
    fi

    # Migration 67: Update nightly-consolidation cron entry to redirect stdout+stderr to a log file.
    # The original entry (added in Migration 61) did not capture output, so errors from the script
    # were silently dropped. This migration replaces it with an entry that appends to
    # ~/lobster-workspace/logs/nightly-consolidation.log.
    local NIGHTLY_CONSOLIDATION_MARKER="# LOBSTER-NIGHTLY-CONSOLIDATION"
    local NIGHTLY_LOG="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/logs/nightly-consolidation.log"
    local DESIRED_ENTRY="0 3 * * * $LOBSTER_DIR/scripts/nightly-consolidation.sh >> $NIGHTLY_LOG 2>&1 $NIGHTLY_CONSOLIDATION_MARKER"
    if crontab -l 2>/dev/null | grep -qF "$NIGHTLY_CONSOLIDATION_MARKER"; then
        if ! crontab -l 2>/dev/null | grep -F "$NIGHTLY_CONSOLIDATION_MARKER" | grep -q ">> "; then
            # Entry exists but lacks log redirect — replace it.
            mkdir -p "$(dirname "$NIGHTLY_LOG")"
            "$LOBSTER_DIR/scripts/cron-manage.sh" add "$NIGHTLY_CONSOLIDATION_MARKER" "$DESIRED_ENTRY"
            substep "Updated nightly-consolidation cron entry to redirect output to $NIGHTLY_LOG"
            migrated=$((migrated + 1))
        else
            substep "nightly-consolidation cron entry already has log redirect — skipping"
        fi
    else
        # Entry is missing entirely — add it with logging.
        mkdir -p "$(dirname "$NIGHTLY_LOG")"
        chmod +x "$LOBSTER_DIR/scripts/nightly-consolidation.sh" 2>/dev/null || true
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$NIGHTLY_CONSOLIDATION_MARKER" "$DESIRED_ENTRY"
        substep "Added nightly-consolidation cron entry with log redirect to $NIGHTLY_LOG"
        migrated=$((migrated + 1))
    fi

    # Migration 68: Broaden context-monitor PostToolUse hook matcher to include Bash
    # (issue #1430). Claude Code only populates context_window in PostToolUse payloads
    # for built-in tools like Bash, not for MCP tool calls. The previous matcher
    # "mcp__lobster-inbox__|Agent" caused the hook to fire but always see no data.
    # Adding "Bash|" to the front ensures the hook receives context_window data.
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local has_bash_in_matcher
        has_bash_in_matcher=$(jq -r '
            [.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("context-monitor")) | .matcher]
            | map(select(startswith("Bash|")))
            | length
        ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
        if [ "${has_bash_in_matcher:-0}" = "0" ] || [ "${has_bash_in_matcher:-0}" = "" ]; then
            TMP_SETTINGS=$(mktemp)
            jq '
                .hooks.PostToolUse = [
                    .hooks.PostToolUse[]? |
                    if (.hooks[]?.command | contains("context-monitor"))
                    then .matcher = ("Bash|" + .matcher)
                    else .
                    end
                ]
            ' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Broadened context-monitor hook matcher to include Bash (issue #1430)"
            migrated=$((migrated + 1))
        else
            substep "context-monitor hook matcher already includes Bash — skipping"
        fi
    fi

    # Migration 69: Install wfm-watchdog.sh cron entry (every 10 minutes).
    # Detects when wait_for_messages() is frozen (running >35 min) and injects
    # a synthetic wfm_watchdog inbox message to unblock the dispatcher.
    local WFM_WATCHDOG_MARKER="# LOBSTER-WFM-WATCHDOG"
    local wfm_watchdog_script="$LOBSTER_DIR/scripts/wfm-watchdog.sh"
    if ! crontab -l 2>/dev/null | grep -qF "$WFM_WATCHDOG_MARKER"; then
        if [[ -f "$wfm_watchdog_script" ]]; then
            "$LOBSTER_DIR/scripts/cron-manage.sh" add \
                "$WFM_WATCHDOG_MARKER" \
                "*/10 * * * * $wfm_watchdog_script $WFM_WATCHDOG_MARKER"
            substep "Added wfm-watchdog.sh cron entry (every 10 minutes)"
            migrated=$((migrated + 1))
        else
            substep "WARN: wfm-watchdog.sh not found at $wfm_watchdog_script — skipping"
        fi
    else
        substep "wfm-watchdog.sh cron entry already present — skipping"
    fi

    # Migration 70: Install piper TTS and lessac-medium voice model for send_voice_note.
    # Soft requirement: failure warns but does not abort upgrade.
    local PIPER_BIN_PATH="/usr/local/bin/piper"
    local PIPER_MODELS_TARGET="${WORKSPACE_DIR}/piper-models"
    local PIPER_MODEL_FILE="${PIPER_MODELS_TARGET}/en_US-lessac-medium.onnx"
    mkdir -p "$PIPER_MODELS_TARGET"

    if [ ! -x "$PIPER_BIN_PATH" ] && ! command -v piper &>/dev/null; then
        substep "Installing piper TTS binary for send_voice_note..."
        local _arch
        _arch="$(uname -m)"
        local _piper_arch=""
        case "$_arch" in
            x86_64)   _piper_arch="amd64" ;;
            aarch64)  _piper_arch="aarch64" ;;
            armv7l)   _piper_arch="armv7" ;;
        esac
        if [ -n "$_piper_arch" ]; then
            local _piper_url
            _piper_url="$(curl -fsSL https://api.github.com/repos/rhasspy/piper/releases/latest 2>/dev/null | \
                python3 -c "import sys,json; \
                data=json.load(sys.stdin); \
                urls=[a['browser_download_url'] for a in data.get('assets',[]) \
                      if 'linux_${_piper_arch}' in a['name'] and a['name'].endswith('.tar.gz')]; \
                print(urls[0] if urls else '')" 2>/dev/null || true)"
            if [ -n "$_piper_url" ]; then
                local _ptmp
                _ptmp="$(mktemp -d)"
                if curl -fsSL -o "${_ptmp}/piper.tar.gz" "$_piper_url" && \
                   tar -xzf "${_ptmp}/piper.tar.gz" -C "$_ptmp"; then
                    local _bin
                    _bin="$(find "$_ptmp" -type f -name "piper" | head -1)"
                    if [ -n "$_bin" ]; then
                        local _bin_dir
                        _bin_dir="$(dirname "$_bin")"
                        sudo cp "$_bin" "$PIPER_BIN_PATH"
                        sudo chmod +x "$PIPER_BIN_PATH"
                        # Copy shared libraries
                        for _lib in libonnxruntime.so.* libpiper_phonemize.so.* libespeak-ng.so.*; do
                            _lib_path="$(find "$_bin_dir" -name "$_lib" -type f | head -1)"
                            [ -n "$_lib_path" ] && sudo cp "$_lib_path" /usr/local/lib/ 2>/dev/null || true
                        done
                        sudo ldconfig 2>/dev/null || true
                        # Install bundled espeak-ng-data
                        if [ -d "${_bin_dir}/espeak-ng-data" ]; then
                            sudo cp -r "${_bin_dir}/espeak-ng-data" /usr/share/ 2>/dev/null || true
                        fi
                        substep "piper TTS installed to $PIPER_BIN_PATH"
                        migrated=$((migrated + 1))
                    fi
                fi
                rm -rf "$_ptmp"
            fi
        fi
    fi

    if [ ! -f "$PIPER_MODEL_FILE" ]; then
        substep "Downloading piper lessac-medium voice model (~30MB)..."
        local _model_url="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
        local _model_json_url="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"
        if curl -fsSL -o "$PIPER_MODEL_FILE" "$_model_url" && \
           curl -fsSL -o "${PIPER_MODEL_FILE}.json" "$_model_json_url"; then
            substep "piper voice model downloaded"
            migrated=$((migrated + 1))
        else
            warn "piper voice model download failed — send_voice_note will fall back to text"
            rm -f "$PIPER_MODEL_FILE" "${PIPER_MODEL_FILE}.json"
        fi
    fi

    # Migration 71: Remove stale LOBSTER-SCHEDULED crontab entries (issue #1083 Phase 1).
    # The cron + jobs.json + dispatch-job.sh scheduling layer has been superseded by
    # systemd timers (PR #1105). Any remaining "# LOBSTER-SCHEDULED" crontab entries
    # are now duplicates of systemd timers or orphaned jobs that no longer fire on
    # systemd. Remove them so the crontab is clean.
    #
    # SAFETY: Only remove a LOBSTER-SCHEDULED cron entry for a job if a corresponding
    # lobster-managed systemd timer already exists for that job. Entries for jobs that
    # have no systemd timer are left in place and a warning is printed. This prevents
    # silent loss of the only trigger for a job.
    #
    # NOTE: System-level cron entries (LOBSTER-HEALTH, LOBSTER-SELF-CHECK, etc.) are
    # intentionally preserved — only LOBSTER-SCHEDULED user-space job entries are removed.
    if crontab -l 2>/dev/null | grep -q '# LOBSTER-SCHEDULED'; then
        _m71_safe_to_remove=""
        _m71_skipped=""
        while IFS= read -r _m71_line; do
            # Extract the job name from lines like:
            #   0 */6 * * * /path/dispatch-job.sh lobstertalk-ssh-watcher # LOBSTER-SCHEDULED
            _m71_job=$(echo "$_m71_line" | grep -oP '(?<=dispatch-job\.sh )\S+' || true)
            if [ -z "$_m71_job" ]; then
                # Not a dispatch-job.sh line — skip it (don't remove)
                _m71_skipped="${_m71_skipped}${_m71_line}\n"
                continue
            fi
            _m71_timer="/etc/systemd/system/lobster-${_m71_job}.timer"
            if [ -f "$_m71_timer" ] && grep -q '# LOBSTER-MANAGED' "$_m71_timer" 2>/dev/null; then
                # Systemd timer exists and is lobster-managed — safe to remove cron entry
                _m71_safe_to_remove="${_m71_safe_to_remove}${_m71_job} "
            else
                # No systemd timer — leave cron entry in place, warn operator
                substep "WARNING: LOBSTER-SCHEDULED cron entry for '${_m71_job}' has no systemd timer — leaving in place"
                substep "  To fix: create a systemd timer for '${_m71_job}' via create_scheduled_job MCP tool, then re-run upgrade.sh"
                _m71_skipped="${_m71_skipped}${_m71_line}\n"
            fi
        done < <(crontab -l 2>/dev/null | grep '# LOBSTER-SCHEDULED')

        if [ -n "$_m71_safe_to_remove" ]; then
            # Build a pattern that matches only the job names we confirmed are timer-backed
            _m71_pattern=$(echo "$_m71_safe_to_remove" | tr ' ' '\n' | grep -v '^$' | sed 's/.*/dispatch-job\\.sh &/' | paste -sd '|')
            { crontab -l 2>/dev/null | grep -Ev "$_m71_pattern" || true; } | crontab -
            substep "Removed LOBSTER-SCHEDULED cron entries for timer-backed jobs: ${_m71_safe_to_remove% }"
            migrated=$((migrated + 1))
        else
            substep "No timer-backed LOBSTER-SCHEDULED cron entries to remove"
        fi
    else
        substep "No LOBSTER-SCHEDULED crontab entries found — skipping"
    fi

    # Migration 72: Enable lobster-claude and lobster-router for autostart on existing installs.
    # Non-interactive installs (NON_INTERACTIVE=true) prior to this fix skipped the
    # `systemctl enable` call entirely, leaving the services installed but not enabled.
    # After any reboot the services would not start automatically, causing ~4 min downtime
    # until the health check detected and restarted the missing session. Fix: enable
    # unconditionally if the service unit is present but not enabled. (issue #1603)
    for _m72_svc in lobster-router lobster-claude; do
        if systemctl list-unit-files --quiet "${_m72_svc}.service" 2>/dev/null | grep -q "^${_m72_svc}"; then
            if ! systemctl is-enabled --quiet "${_m72_svc}" 2>/dev/null; then
                sudo systemctl enable "${_m72_svc}" 2>/dev/null || true
                substep "Enabled ${_m72_svc} for autostart"
                migrated=$((migrated + 1))
            else
                substep "${_m72_svc} already enabled — skipping"
            fi
        else
            substep "${_m72_svc}.service not found — skipping"
        fi
    done

    # Migration 73: Remove stale system-audit.context.md from memory/canonical/
    # install.sh's generic canonical-template loop previously copied system-audit.context.md
    # to both memory/canonical/ and agents/ (the latter via a dedicated block).
    # The agents/ copy is the canonical write target — the memory/canonical/ copy was
    # never updated by the lobster-auditor and drifted stale. Fix: delete the stale copy
    # and exclude it from the generic loop going forward (issue #1196).
    local stale_audit_context="$USER_CONFIG_DIR/memory/canonical/system-audit.context.md"
    if [ -f "$stale_audit_context" ]; then
        rm -f "$stale_audit_context"
        substep "Removed stale system-audit.context.md from memory/canonical/ (canonical copy is agents/system-audit.context.md)"
        migrated=$((migrated + 1))
    fi

    # Migration 74: Enable and start lobster-transcription.service on existing installs.
    # Prior to this fix, install.sh installed the service file but never called
    # systemctl enable, so voice messages accumulated in pending-transcription/ forever.
    if systemctl is-system-running >/dev/null 2>&1 || pidof systemd >/dev/null 2>&1; then
        local transcription_svc="$LOBSTER_DIR/services/lobster-transcription.service"
        if [ -f "$transcription_svc" ]; then
            sudo cp "$transcription_svc" /etc/systemd/system/lobster-transcription.service
            sudo systemctl daemon-reload 2>/dev/null || true
            if ! systemctl is-enabled --quiet lobster-transcription 2>/dev/null; then
                sudo systemctl enable lobster-transcription 2>/dev/null || true
                substep "Enabled lobster-transcription.service"
                migrated=$((migrated + 1))
            fi
            if ! systemctl is-active --quiet lobster-transcription 2>/dev/null; then
                sudo systemctl start lobster-transcription 2>/dev/null || true
                substep "Started lobster-transcription.service"
                migrated=$((migrated + 1))
            fi
        else
            substep "WARN: lobster-transcription.service not found at $transcription_svc — skipping"
        fi
    else
        substep "systemd not running — skipping lobster-transcription.service enable (container?)"
    fi

    # Migration 75: Install LOBSTER-CLEANUP cron entry (worktree + audio cleanup, issue #1609).
    # cleanup-worktrees-audio.sh prunes finished git worktrees and removes audio files
    # older than 7 days. Runs daily at 04:00 to avoid overlap with nightly consolidation (03:00).
    local CLEANUP_MARKER="# LOBSTER-CLEANUP"
    local CLEANUP_SCRIPT="$LOBSTER_DIR/scripts/cleanup-worktrees-audio.sh"
    if [ -f "$CLEANUP_SCRIPT" ]; then
        chmod +x "$CLEANUP_SCRIPT" 2>/dev/null || true
        if ! crontab -l 2>/dev/null | grep -qF "$CLEANUP_MARKER"; then
            "$LOBSTER_DIR/scripts/cron-manage.sh" add "$CLEANUP_MARKER" \
                "0 4 * * * $CLEANUP_SCRIPT >> $HOME/lobster-workspace/logs/cleanup.log 2>&1 $CLEANUP_MARKER" 2>/dev/null && {
                substep "Added LOBSTER-CLEANUP cron entry (cleanup-worktrees-audio.sh, 04:00 daily)"
                migrated=$((migrated + 1))
            } || warn "Could not add LOBSTER-CLEANUP cron entry — check cron-manage.sh"
        fi
    else
        warn "cleanup-worktrees-audio.sh not found at $CLEANUP_SCRIPT — skipping Migration 75"
    fi

    # Migration 76: Remove wfm-watchdog.sh cron entry (superseded by PR #1646).
    # PR #1646 fixed the actual root cause: the health check now treats a fresh
    # wfm-active signal as GREEN, so the false-positive kills the watchdog was
    # designed to work around no longer occur. The watchdog now only generates
    # noise during normal idle operation.
    local WFM_WATCHDOG_REMOVE_MARKER="# LOBSTER-WFM-WATCHDOG"
    if crontab -l 2>/dev/null | grep -qF "$WFM_WATCHDOG_REMOVE_MARKER"; then
        "$LOBSTER_DIR/scripts/cron-manage.sh" remove "$WFM_WATCHDOG_REMOVE_MARKER" 2>/dev/null && {
            substep "Removed wfm-watchdog.sh cron entry (superseded by PR #1646)"
            migrated=$((migrated + 1))
        } || warn "Could not remove LOBSTER-WFM-WATCHDOG cron entry — remove manually"
    else
        substep "wfm-watchdog.sh cron entry not present — nothing to remove"
    fi

    # Migration 77: Add permissions.defaultMode bypassPermissions to settings.json (issue #1706).
    # Claude Code has a known regression where --dangerously-skip-permissions (CLI flag) stops
    # working after auto-updates. Setting permissions.defaultMode in settings.json is the
    # permanent fix that survives updates.
    if [ -f "$CLAUDE_SETTINGS" ]; then
        if jq -e '.permissions.defaultMode != "bypassPermissions"' "$CLAUDE_SETTINGS" > /dev/null 2>&1; then
            substep "Adding permissions.defaultMode: bypassPermissions to settings.json..."
            jq '. + {"skipDangerousModePermissionPrompt": true, "permissions": {"defaultMode": "bypassPermissions"}}' "$CLAUDE_SETTINGS" > "$CLAUDE_SETTINGS.tmp" && mv "$CLAUDE_SETTINGS.tmp" "$CLAUDE_SETTINGS"
            success "Permissions bypass settings added"
            migrated=$((migrated + 1))
        fi
    else
        warn "Claude settings not found at $CLAUDE_SETTINGS — skipping Migration 77"
    fi

    # Migration 78: Remove stale dispatch-job.sh LOBSTER-SCHEDULED cron entries.
    # These three entries were already superseded by systemd timers but Migration 71
    # left them in place on installs where the timer check was inconclusive.
    # Two entries use invalid systemd-style cron syntax (*-*-* ...) that standard
    # cron ignores entirely; the third (lobstertalk-ssh-watcher) fires every 6h
    # and causes duplicate invocations alongside the timer. Remove all three
    # unconditionally — the systemd timers are the canonical trigger.
    _m78_jobs="lobstertalk-unified lobstertalk-ssh-watcher lobstertalk-kanban-watcher"
    _m78_removed=""
    for _m78_job in $_m78_jobs; do
        if crontab -l 2>/dev/null | grep -q "dispatch-job\.sh ${_m78_job}"; then
            { crontab -l 2>/dev/null | grep -v "dispatch-job\.sh ${_m78_job}" || true; } | crontab -
            _m78_removed="${_m78_removed}${_m78_job} "
            substep "Removed stale LOBSTER-SCHEDULED cron entry for ${_m78_job}"
        fi
    done
    if [ -n "$_m78_removed" ]; then
        success "Migration 78: removed cron entries for: ${_m78_removed% }"
        migrated=$((migrated + 1))
    fi

    # Migration 79: Config consolidation (issue #1785, Option A).
    # Two steps:
    #   a) Merge non-comment, non-duplicate keys from global.env into config.env,
    #      then archive global.env as global.env.bak (safe rollback).
    #   b) Remove stale duplicate lobster/config/consolidation.conf and
    #      lobster/config/sync-repos.json left by the original migration 0.
    local _m79_config_env="$LOBSTER_CONFIG_DIR/config.env"
    local _m79_global_env="$LOBSTER_CONFIG_DIR/global.env"

    # Step a: merge global.env → config.env
    if [ -f "$_m79_global_env" ] && [ ! -f "${_m79_global_env}.bak" ]; then
        local _m79_merged=0
        while IFS= read -r _m79_line; do
            # Skip comments and blank lines
            [[ "$_m79_line" =~ ^[[:space:]]*# ]] && continue
            [[ -z "${_m79_line// }" ]] && continue

            # Extract key (everything before first '=')
            local _m79_key
            _m79_key="${_m79_line%%=*}"
            [ -z "$_m79_key" ] && continue

            # Skip if key already exists in config.env
            if grep -qE "^${_m79_key}=" "$_m79_config_env" 2>/dev/null; then
                substep "  global.env: ${_m79_key} already in config.env — skipping"
                continue
            fi

            # Append to config.env
            echo "$_m79_line" >> "$_m79_config_env"
            substep "  global.env: merged ${_m79_key} into config.env"
            _m79_merged=$((_m79_merged + 1))
        done < "$_m79_global_env"

        # Archive global.env (keep as .bak for safety — delete after next stable release)
        mv "$_m79_global_env" "${_m79_global_env}.bak"
        substep "Archived global.env to global.env.bak ($_m79_merged keys merged into config.env)"
        migrated=$((migrated + 1))
    else
        substep "global.env already migrated or absent — skipping step a"
    fi

    # Step b: remove stale duplicate files in the repo's config/ directory
    local _m79_repo_conf="$LOBSTER_DIR/config/consolidation.conf"
    local _m79_repo_repos="$LOBSTER_DIR/config/sync-repos.json"
    if [ -f "$_m79_repo_conf" ]; then
        rm -f "$_m79_repo_conf"
        substep "Removed stale $LOBSTER_DIR/config/consolidation.conf"
        migrated=$((migrated + 1))
    fi
    if [ -f "$_m79_repo_repos" ]; then
        rm -f "$_m79_repo_repos"
        substep "Removed stale $LOBSTER_DIR/config/sync-repos.json"
        migrated=$((migrated + 1))
    fi


    # Migration 80: Disable Gmail Pub/Sub systemd timers (issue #1807).
    # The Pub/Sub-based email pipeline (gmail-watch-renewal + awp-gmail-token-refresh)
    # is replaced by the deterministic gmail-poll.py History API poller, which runs
    # every 10 seconds with zero token spend on empty polls. No GCP setup required.
    for _m80_unit in lobster-gmail-watch-renewal lobster-awp-gmail-token-refresh; do
        if systemctl is-enabled "${_m80_unit}.timer" &>/dev/null; then
            substep "Disabling ${_m80_unit}.timer (Pub/Sub pipeline, superseded by gmail-poll.py)..."
            sudo systemctl disable --now "${_m80_unit}.timer" 2>/dev/null && {
                substep "Disabled ${_m80_unit}.timer"
                migrated=$((migrated + 1))
            } || warn "Could not disable ${_m80_unit}.timer -- disable manually"
        else
            substep "${_m80_unit}.timer already disabled -- nothing to do"
        fi
    done

    # Migration 81: Install PreToolUse heartbeat hook (issue #1786).
    # pre-tool-heartbeat.py writes a timestamp before each tool call, complementing
    # thinking-heartbeat.py (PostToolUse). Together they allow the health check to
    # distinguish "tool is running (long)" from "dispatcher is frozen" without
    # false positives, enabling the PostToolUse threshold to be lowered safely.
    if [ -f "$CLAUDE_SETTINGS" ] && command -v jq >/dev/null 2>&1; then
        local _m81_hook_path="$LOBSTER_DIR/hooks/pre-tool-heartbeat.py"
        if [ -f "$_m81_hook_path" ]; then
            chmod +x "$_m81_hook_path" 2>/dev/null || true
            local _m81_present
            _m81_present=$(jq -r '
                [.hooks.PreToolUse[]?.hooks[]?.command // empty]
                | map(select(contains("pre-tool-heartbeat")))
                | length
            ' "$CLAUDE_SETTINGS" 2>/dev/null || echo "0")
            if [ "${_m81_present:-0}" = "0" ] || [ "${_m81_present:-0}" = "" ]; then
                TMP_SETTINGS=$(mktemp)
                jq --arg cmd "python3 $LOBSTER_DIR/hooks/pre-tool-heartbeat.py" \
                   '.hooks.PreToolUse = (.hooks.PreToolUse // []) + [{
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": $cmd,
                        "timeout": 5
                    }]
                }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
                substep "Registered pre-tool-heartbeat hook in Claude Code settings"
                migrated=$((migrated + 1))
            else
                substep "pre-tool-heartbeat hook already present — skipping Migration 81"
            fi
        else
            warn "pre-tool-heartbeat.py not found at $_m81_hook_path — skipping Migration 81"
        fi
    else
        warn "Claude settings not found at $CLAUDE_SETTINGS or jq missing — skipping Migration 81"
    fi

    if [ "$migrated" -eq 0 ]; then
        success "No migrations needed"
    else
        success "$migrated migration(s) applied"
    fi

    log_to_file "Migration check complete, $migrated migrations applied"
}

#===============================================================================
# 10. Health check
#===============================================================================

health_check() {
    step "Running health check"

    if $DRY_RUN; then
        info "[dry-run] Would run health checks"
        return 0
    fi

    local checks_passed=0
    local checks_failed=0

    # Check 1: Install integrity
    cd "$LOBSTER_DIR"
    if [ "$INSTALL_MODE" = "git" ]; then
        local branch
        branch=$(git branch --show-current 2>/dev/null || echo "unknown")
        if [ "$branch" = "main" ]; then
            success "On branch: main"
            checks_passed=$((checks_passed + 1))
        else
            warn "Not on main branch (on: $branch)"
            checks_failed=$((checks_failed + 1))
        fi
    else
        if [ -f "$LOBSTER_DIR/VERSION" ]; then
            local ver
            ver=$(cat "$LOBSTER_DIR/VERSION")
            success "Tarball install: v$ver"
            checks_passed=$((checks_passed + 1))
        else
            warn "VERSION file missing"
            checks_failed=$((checks_failed + 1))
        fi
    fi

    # Check 2: Config file exists and has token
    if [ -f "$CONFIG_FILE" ]; then
        if grep -q "TELEGRAM_BOT_TOKEN" "$CONFIG_FILE" 2>/dev/null; then
            success "Config file valid"
            checks_passed=$((checks_passed + 1))
        else
            warn "Config file missing TELEGRAM_BOT_TOKEN"
            checks_failed=$((checks_failed + 1))
        fi
    else
        warn "Config file not found at $CONFIG_FILE"
        checks_failed=$((checks_failed + 1))
    fi

    # Check 3: Venv and key packages
    if [ -d "$VENV_DIR" ]; then
        # shellcheck source=/dev/null
        source "$VENV_DIR/bin/activate"
        local missing_pkgs=()
        for pkg in mcp telegram watchdog; do
            if ! python -c "import $pkg" 2>/dev/null; then
                missing_pkgs+=("$pkg")
            fi
        done
        deactivate

        if [ ${#missing_pkgs[@]} -eq 0 ]; then
            success "Core Python packages installed"
            checks_passed=$((checks_passed + 1))
        else
            warn "Missing Python packages: ${missing_pkgs[*]}"
            checks_failed=$((checks_failed + 1))
        fi
    else
        warn "Python venv not found"
        checks_failed=$((checks_failed + 1))
    fi

    # Check 4: Required directories exist
    local required_dirs=("$MESSAGES_DIR/inbox" "$MESSAGES_DIR/outbox" "$MESSAGES_DIR/processed" "$MESSAGES_DIR/sent")
    local all_dirs_ok=true
    for dir in "${required_dirs[@]}"; do
        if [ ! -d "$dir" ]; then
            all_dirs_ok=false
            break
        fi
    done
    if $all_dirs_ok; then
        success "Message directories OK"
        checks_passed=$((checks_passed + 1))
    else
        warn "Some message directories missing"
        checks_failed=$((checks_failed + 1))
    fi

    # Check 5: Services running
    for svc in lobster-router lobster-claude; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            success "$svc is running"
            checks_passed=$((checks_passed + 1))
        elif systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            warn "$svc is enabled but not running"
            checks_failed=$((checks_failed + 1))
        else
            info "$svc not configured (OK if running manually)"
        fi
    done

    # Check 6: MCP server registered with Claude
    if command -v claude &>/dev/null; then
        if claude mcp list 2>/dev/null | grep -q "lobster-inbox"; then
            success "MCP server registered with Claude"
            checks_passed=$((checks_passed + 1))
        else
            warn "MCP server 'lobster-inbox' not registered with Claude"
            checks_failed=$((checks_failed + 1))
        fi
    fi

    # Check 7: Playwright/Chromium (optional)
    if ! $SKIP_PLAYWRIGHT; then
        if [ -d "$HOME/.cache/ms-playwright" ] && ls "$HOME/.cache/ms-playwright"/chromium-* &>/dev/null 2>&1; then
            success "Playwright Chromium available"
            checks_passed=$((checks_passed + 1))
        else
            info "Playwright Chromium not installed (fetch_page will not work)"
        fi
    fi

    # Check 8: Telegram API reachable (if token available)
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null
        source "$CONFIG_FILE" 2>/dev/null || true
        if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
            if curl -s --connect-timeout 5 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null | grep -q '"ok":true'; then
                success "Telegram API reachable"
                checks_passed=$((checks_passed + 1))
            else
                warn "Telegram API check failed"
                checks_failed=$((checks_failed + 1))
            fi
        fi
    fi

    echo ""
    info "Health check: $checks_passed passed, $checks_failed warnings"

    if [ "$checks_failed" -gt 0 ] && ! $FORCE; then
        warn "Some checks had warnings. Use --force to ignore."
    fi

    log_to_file "Health check: $checks_passed passed, $checks_failed warnings"
}

#===============================================================================
# Main
#===============================================================================

main() {
    parse_args "$@"

    local start_time
    start_time=$(date +%s)

    echo -e "${BLUE}${BOLD}"
    echo "================================================================="
    echo "                    LOBSTER UPGRADE"
    echo "================================================================="
    echo -e "${NC}"

    if $DRY_RUN; then
        echo -e "${YELLOW}${BOLD}  DRY RUN MODE - no changes will be made${NC}"
        echo ""
    fi

    acquire_lock
    trap cleanup_lock EXIT

    preflight_checks          # 0. Pre-flight
    backup_config             # 1. Backup
    git_pull                  # 2. Git pull
    show_whats_new            # 2b. Show what's new
    update_python_deps        # 3. Python deps
    create_new_directories    # 4. New directories
    setup_syncthing           # 5. Syncthing (optional/prompted)
    install_playwright        # 6. Playwright/Chromium
    update_systemd_services   # 8. Systemd updates
    restart_services          # 7. Service restarts
    run_migrations            # 9. Migrations
    health_check              # 10. Health check

    local elapsed=$(( $(date +%s) - start_time ))

    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "================================================================="
    echo "                    UPGRADE COMPLETE"
    echo "================================================================="
    echo -e "${NC}"
    echo ""
    info "Time: ${elapsed}s"
    info "Commit: $PREVIOUS_COMMIT -> $CURRENT_COMMIT"
    if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
        info "Backup: $BACKUP_DIR"
    fi
    if [ "$WARNINGS" -gt 0 ]; then
        warn "$WARNINGS warning(s) during upgrade"
    fi
    if [ "$ERRORS" -gt 0 ]; then
        error "$ERRORS error(s) during upgrade"
    fi
    echo ""

    cleanup_lock
}

main "$@"
