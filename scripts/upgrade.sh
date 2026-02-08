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
BACKUP_BASE="$HOME/lobster-backups"
CONFIG_FILE="$LOBSTER_DIR/config/config.env"
LOCK_FILE="/tmp/lobster-upgrade.lock"
VENV_DIR="$LOBSTER_DIR/.venv"

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

    # Lobster repo must exist
    if [ ! -d "$LOBSTER_DIR/.git" ]; then
        die "Lobster repo not found at $LOBSTER_DIR. Is Lobster installed?" 3
    fi
    success "Lobster repo found at $LOBSTER_DIR"

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

    # Record current commit
    cd "$LOBSTER_DIR"
    PREVIOUS_COMMIT=$(git rev-parse --short HEAD)
    info "Current commit: $PREVIOUS_COMMIT"
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
        "$LOBSTER_DIR/config/config.env"
        "$LOBSTER_DIR/config/lobster.conf"
        "$LOBSTER_DIR/scheduled-tasks/jobs.json"
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

    log_to_file "Git updated: $PREVIOUS_COMMIT -> $CURRENT_COMMIT"
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
        "$MESSAGES_DIR/sent"
        "$MESSAGES_DIR/files"
        "$MESSAGES_DIR/images"
        "$MESSAGES_DIR/audio"
        "$MESSAGES_DIR/config"
        "$MESSAGES_DIR/task-outputs"
        "$LOBSTER_DIR/scheduled-tasks/tasks"
        "$LOBSTER_DIR/scheduled-tasks/logs"
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
    read -r -p "$(echo -e "${CYAN}Install and configure Syncthing? [y/N]:${NC} ")" response
    echo ""

    if [[ ! "$response" =~ ^[Yy]$ ]]; then
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

    # Migration 1: Old config location (~/.lobster.env -> config/config.env)
    if [ -f "$HOME/.lobster.env" ] && [ ! -f "$CONFIG_FILE" ]; then
        substep "Migrating .lobster.env to config/config.env..."
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$HOME/.lobster.env" "$CONFIG_FILE"
        success "Config migrated from ~/.lobster.env"
        migrated=$((migrated + 1))
    fi

    # Migration 2: Old .env in repo root -> config/config.env
    if [ -f "$LOBSTER_DIR/.env" ] && [ ! -f "$CONFIG_FILE" ]; then
        substep "Migrating .env to config/config.env..."
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$LOBSTER_DIR/.env" "$CONFIG_FILE"
        success "Config migrated from .env"
        migrated=$((migrated + 1))
    fi

    # Migration 3: Hyperion -> Lobster rename (old service names)
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
        mkdir -p "$MESSAGES_DIR"/{inbox,outbox,processed,sent,files,images,audio,config,task-outputs}
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

    # Check 1: Git repo is clean and on main
    cd "$LOBSTER_DIR"
    local branch
    branch=$(git branch --show-current 2>/dev/null || echo "unknown")
    if [ "$branch" = "main" ]; then
        success "On branch: main"
        checks_passed=$((checks_passed + 1))
    else
        warn "Not on main branch (on: $branch)"
        checks_failed=$((checks_failed + 1))
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
