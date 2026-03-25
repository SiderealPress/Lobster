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
            jq --arg cmd "python3 $INSTALL_DIR/hooks/system-file-protect.py" \
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
            jq --arg cmd "python3 $INSTALL_DIR/hooks/require-auditor-context-update.py" \
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
            chmod +x "$INSTALL_DIR/hooks/secret-scanner.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq --arg cmd "python3 $INSTALL_DIR/hooks/secret-scanner.py" \
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
    if ! crontab -l 2>/dev/null | grep -q "$LOG_EXPORT_MARKER"; then
        local log_export_script="$LOBSTER_DIR/scheduled-tasks/export-logs.py"
        chmod +x "$log_export_script" 2>/dev/null || true
        "$LOBSTER_DIR/scripts/cron-manage.sh" add "$LOG_EXPORT_MARKER" \
            "0 3 * * * cd $LOBSTER_DIR && uv run scheduled-tasks/export-logs.py $LOG_EXPORT_MARKER"
        substep "Added daily log-export cron entry (03:00 UTC, archives observations.log + audit.jsonl)"
        migrated=$((migrated + 1))
    fi

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
            chmod +x "$INSTALL_DIR/hooks/require-wait-for-messages.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq '.hooks.Stop = (.hooks.Stop // []) + [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "python3 '"$INSTALL_DIR"'/hooks/require-wait-for-messages.py",
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
            chmod +x "$INSTALL_DIR/hooks/on-fresh-start.py" 2>/dev/null || true
            TMP_SETTINGS=$(mktemp)
            jq '.hooks.SessionStart = (.hooks.SessionStart // []) + [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": "python3 '"$INSTALL_DIR"'/hooks/on-fresh-start.py",
                    "timeout": 30
                }]
            }]' "$CLAUDE_SETTINGS" > "$TMP_SETTINGS" && mv "$TMP_SETTINGS" "$CLAUDE_SETTINGS"
            substep "Registered on-fresh-start SessionStart hook in settings.json"
            migrated=$((migrated + 1))
        fi
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
