#!/bin/bash
#===============================================================================
# Lobster Update Script
#
# Safely updates Lobster: pulls repo changes, restarts services, and updates
# Claude Code CLI - all without data loss or significant downtime.
#
# Usage: ./update-lobster.sh [--force] [--skip-claude] [--dry-run] [--rollback]
#
# Options:
#   --force        Continue even if health checks fail
#   --skip-claude  Skip Claude Code CLI update
#   --dry-run      Show what would happen without making changes
#   --rollback     Restore from most recent backup
#
# Exit codes:
#   0 - Success
#   1 - General error
#   2 - Lock file exists (another update running)
#   3 - Pre-flight check failed
#   4 - Backup failed
#   5 - Git update failed
#   6 - Service restart failed
#   7 - Health check failed (unless --force)
#===============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Directories
LOBSTER_DIR="$HOME/lobster"
WORKSPACE_DIR="$HOME/lobster-workspace"
BACKUP_BASE="$HOME/lobster-backups"
MESSAGES_DIR="$HOME/messages"
CONFIG_FILE="$LOBSTER_DIR/config/config.env"

# Lock file
LOCK_FILE="/tmp/lobster-update.lock"

# Services (in stop order - daemon first, then router)
SERVICES_STOP=("lobster-daemon" "lobster-router")
# Start order is reversed
SERVICES_START=("lobster-router" "lobster-daemon")

# State files to backup
STATE_FILES=(
    "$WORKSPACE_DIR/.lobster_session_id"
    "$MESSAGES_DIR/tasks.json"
    "$LOBSTER_DIR/scheduled-tasks/jobs.json"
    "$CONFIG_FILE"
)

# Options
FORCE=false
SKIP_CLAUDE=false
DRY_RUN=false
ROLLBACK=false

# Globals
BACKUP_DIR=""
LOG_FILE=""
PREVIOUS_COMMIT=""
CURRENT_COMMIT=""
START_TIME=""

#-------------------------------------------------------------------------------
# Logging
#-------------------------------------------------------------------------------

log() {
    local level="$1"
    shift
    local msg="$*"
    local ts=$(date '+%Y-%m-%d %H:%M:%S')

    case "$level" in
        INFO)  echo -e "${BLUE}[INFO]${NC} $msg" ;;
        OK)    echo -e "${GREEN}[OK]${NC} $msg" ;;
        WARN)  echo -e "${YELLOW}[WARN]${NC} $msg" ;;
        ERROR) echo -e "${RED}[ERROR]${NC} $msg" ;;
        STEP)  echo -e "\n${CYAN}${BOLD}>>> $msg${NC}" ;;
    esac

    # Also write to log file if it exists
    if [ -n "$LOG_FILE" ] && [ -f "$LOG_FILE" ]; then
        echo "[$ts] [$level] $msg" >> "$LOG_FILE"
    fi
}

die() {
    log ERROR "$1"
    cleanup_lock
    exit "${2:-1}"
}

#-------------------------------------------------------------------------------
# Lock management
#-------------------------------------------------------------------------------

acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
        if [ "$pid" != "unknown" ] && kill -0 "$pid" 2>/dev/null; then
            die "Another update is running (PID: $pid). Remove $LOCK_FILE if stale." 2
        else
            log WARN "Stale lock file found, removing..."
            rm -f "$LOCK_FILE"
        fi
    fi
    echo $$ > "$LOCK_FILE"
}

cleanup_lock() {
    rm -f "$LOCK_FILE"
}

#-------------------------------------------------------------------------------
# Pre-flight checks
#-------------------------------------------------------------------------------

preflight_checks() {
    log STEP "Pre-flight checks"

    # Check we're in the right directory
    if [ ! -d "$LOBSTER_DIR/.git" ]; then
        die "Lobster repo not found at $LOBSTER_DIR" 3
    fi

    # Check internet connectivity
    if ! curl -s --connect-timeout 5 https://api.github.com >/dev/null 2>&1; then
        die "No internet connectivity" 3
    fi
    log OK "Internet connectivity confirmed"

    # Check disk space (need at least 100MB)
    local free_kb=$(df "$HOME" | awk 'NR==2 {print $4}')
    if [ "$free_kb" -lt 102400 ]; then
        die "Insufficient disk space (need at least 100MB free)" 3
    fi
    log OK "Disk space OK ($(( free_kb / 1024 ))MB free)"

    # Check git status for uncommitted changes
    cd "$LOBSTER_DIR"
    if [ -n "$(git status --porcelain)" ]; then
        log WARN "Local changes detected in repo"
        if $DRY_RUN; then
            log INFO "Would stash local changes"
        fi
    fi

    # Check if updates are available
    git fetch origin main --quiet 2>/dev/null || die "Failed to fetch from origin" 3
    local behind=$(git rev-list HEAD..origin/main --count 2>/dev/null || echo "0")

    if [ "$behind" = "0" ]; then
        log INFO "Already up to date"
        if ! $FORCE && ! $ROLLBACK; then
            log OK "No updates available"
            cleanup_lock
            exit 0
        fi
    else
        log OK "$behind commit(s) available from origin/main"
    fi

    PREVIOUS_COMMIT=$(git rev-parse --short HEAD)
    log OK "Current commit: $PREVIOUS_COMMIT"
}

#-------------------------------------------------------------------------------
# Backup
#-------------------------------------------------------------------------------

create_backup() {
    log STEP "Creating backup"

    local timestamp=$(date '+%Y%m%d-%H%M%S')
    BACKUP_DIR="$BACKUP_BASE/backup-$timestamp"
    LOG_FILE="$BACKUP_BASE/update-$timestamp.log"

    if $DRY_RUN; then
        log INFO "Would create backup at $BACKUP_DIR"
        return 0
    fi

    mkdir -p "$BACKUP_DIR"
    echo "Update started at $(date)" > "$LOG_FILE"

    # Backup state files
    for file in "${STATE_FILES[@]}"; do
        if [ -f "$file" ]; then
            local rel_path="${file#$HOME/}"
            local backup_path="$BACKUP_DIR/$rel_path"
            mkdir -p "$(dirname "$backup_path")"
            cp "$file" "$backup_path"
            log OK "Backed up: $rel_path"
        fi
    done

    # Backup processed messages directory (just list, not full copy - too large)
    if [ -d "$MESSAGES_DIR/processed" ]; then
        local processed_count=$(find "$MESSAGES_DIR/processed" -name "*.json" -type f | wc -l)
        echo "$processed_count" > "$BACKUP_DIR/processed-count.txt"
        log OK "Processed message count: $processed_count"
    fi

    # Save git commit hash
    echo "$PREVIOUS_COMMIT" > "$BACKUP_DIR/git-commit.txt"

    # Save Claude CLI version
    if command -v claude &>/dev/null; then
        claude --version 2>/dev/null > "$BACKUP_DIR/claude-version.txt" || echo "unknown" > "$BACKUP_DIR/claude-version.txt"
    fi

    # Save service states
    for service in "${SERVICES_STOP[@]}"; do
        if systemctl is-active --quiet "$service" 2>/dev/null; then
            echo "active" > "$BACKUP_DIR/${service}.state"
        else
            echo "inactive" > "$BACKUP_DIR/${service}.state"
        fi
    done

    log OK "Backup created at $BACKUP_DIR"
}

#-------------------------------------------------------------------------------
# Service management
#-------------------------------------------------------------------------------

stop_services() {
    log STEP "Stopping services"

    for service in "${SERVICES_STOP[@]}"; do
        if systemctl is-active --quiet "$service" 2>/dev/null; then
            if $DRY_RUN; then
                log INFO "Would stop $service"
            else
                log INFO "Stopping $service..."

                # For daemon, wait for Claude to finish current work
                if [ "$service" = "lobster-daemon" ]; then
                    # Send SIGTERM and wait up to 60 seconds
                    sudo systemctl stop "$service" --no-block 2>/dev/null || true

                    local wait_count=0
                    while systemctl is-active --quiet "$service" 2>/dev/null && [ $wait_count -lt 60 ]; do
                        sleep 1
                        ((wait_count++))
                        if [ $((wait_count % 10)) -eq 0 ]; then
                            log INFO "Waiting for daemon to finish... (${wait_count}s)"
                        fi
                    done

                    if systemctl is-active --quiet "$service" 2>/dev/null; then
                        log WARN "Daemon didn't stop gracefully, forcing..."
                        sudo systemctl kill "$service" 2>/dev/null || true
                        sleep 2
                    fi
                else
                    sudo systemctl stop "$service" 2>/dev/null || true
                fi

                if ! systemctl is-active --quiet "$service" 2>/dev/null; then
                    log OK "$service stopped"
                else
                    log WARN "$service may not have stopped cleanly"
                fi
            fi
        else
            log INFO "$service was not running"
        fi
    done
}

start_services() {
    log STEP "Starting services"

    local failed=false

    for service in "${SERVICES_START[@]}"; do
        if $DRY_RUN; then
            log INFO "Would start $service"
        else
            log INFO "Starting $service..."
            if sudo systemctl start "$service" 2>/dev/null; then
                sleep 2
                if systemctl is-active --quiet "$service" 2>/dev/null; then
                    log OK "$service started"
                else
                    log ERROR "$service failed to start"
                    failed=true
                fi
            else
                log ERROR "Failed to start $service"
                failed=true
            fi
        fi
    done

    if $failed; then
        return 1
    fi
    return 0
}

#-------------------------------------------------------------------------------
# Git update
#-------------------------------------------------------------------------------

git_update() {
    log STEP "Updating repository"

    cd "$LOBSTER_DIR"

    # Stash any local changes
    if [ -n "$(git status --porcelain)" ]; then
        if $DRY_RUN; then
            log INFO "Would stash local changes"
        else
            log INFO "Stashing local changes..."
            git stash push -m "lobster-update-$(date +%Y%m%d-%H%M%S)" --quiet
        fi
    fi

    if $DRY_RUN; then
        log INFO "Would pull from origin/main"
        CURRENT_COMMIT=$(git rev-parse --short origin/main)
        log INFO "Would update to: $CURRENT_COMMIT"
        return 0
    fi

    # Try fast-forward merge
    log INFO "Pulling updates..."
    if git merge origin/main --ff-only --quiet 2>/dev/null; then
        CURRENT_COMMIT=$(git rev-parse --short HEAD)
        log OK "Updated to: $CURRENT_COMMIT"

        # Show what changed
        local commits=$(git log --oneline "$PREVIOUS_COMMIT..$CURRENT_COMMIT" 2>/dev/null | head -5)
        if [ -n "$commits" ]; then
            log INFO "Recent changes:"
            echo "$commits" | while read -r line; do
                echo "    $line"
            done
        fi
    else
        log ERROR "Fast-forward merge failed - conflicts detected"
        return 1
    fi
}

#-------------------------------------------------------------------------------
# Dependencies
#-------------------------------------------------------------------------------

update_dependencies() {
    log STEP "Checking dependencies"

    cd "$LOBSTER_DIR"

    # Check if requirements.txt changed
    if git diff --name-only "$PREVIOUS_COMMIT..HEAD" 2>/dev/null | grep -q "requirements.txt"; then
        log INFO "requirements.txt changed, updating Python packages..."

        if $DRY_RUN; then
            log INFO "Would update pip packages"
        else
            if [ -d ".venv" ]; then
                source .venv/bin/activate
                pip install --quiet --upgrade pip
                pip install --quiet -r requirements.txt 2>/dev/null || pip install --quiet mcp python-telegram-bot watchdog python-dotenv
                deactivate
                log OK "Python packages updated"
            else
                log WARN "No venv found, skipping pip update"
            fi
        fi
    else
        log OK "No dependency changes"
    fi
}

#-------------------------------------------------------------------------------
# Claude CLI update
#-------------------------------------------------------------------------------

update_claude_cli() {
    if $SKIP_CLAUDE; then
        log INFO "Skipping Claude CLI update (--skip-claude)"
        return 0
    fi

    log STEP "Updating Claude Code CLI"

    if $DRY_RUN; then
        log INFO "Would run Claude CLI installer"
        return 0
    fi

    # The official installer is idempotent
    log INFO "Running Claude CLI installer..."
    if curl -fsSL https://claude.ai/install.sh 2>/dev/null | sh 2>/dev/null; then
        log OK "Claude CLI updated"
    else
        log WARN "Claude CLI update failed (continuing anyway)"
    fi
}

#-------------------------------------------------------------------------------
# Systemd update
#-------------------------------------------------------------------------------

update_systemd() {
    log STEP "Checking systemd services"

    cd "$LOBSTER_DIR"

    # Check if service files changed
    local changed=false
    if git diff --name-only "$PREVIOUS_COMMIT..HEAD" 2>/dev/null | grep -q "services/"; then
        changed=true
    fi

    if $changed; then
        log INFO "Service files changed, updating..."

        if $DRY_RUN; then
            log INFO "Would copy service files and reload systemd"
        else
            for svc_file in services/*.service; do
                if [ -f "$svc_file" ]; then
                    local svc_name=$(basename "$svc_file")
                    sudo cp "$svc_file" "/etc/systemd/system/$svc_name"
                    log OK "Updated $svc_name"
                fi
            done

            sudo systemctl daemon-reload
            log OK "Systemd daemon reloaded"
        fi
    else
        log OK "No service file changes"
    fi
}

#-------------------------------------------------------------------------------
# Update CLI
#-------------------------------------------------------------------------------

update_cli() {
    log STEP "Updating CLI"

    cd "$LOBSTER_DIR"

    if $DRY_RUN; then
        log INFO "Would update /usr/local/bin/lobster"
        return 0
    fi

    if [ -f "src/cli" ]; then
        sudo cp "src/cli" "/usr/local/bin/lobster"
        sudo chmod +x "/usr/local/bin/lobster"
        log OK "CLI updated"
    fi
}

#-------------------------------------------------------------------------------
# Health checks
#-------------------------------------------------------------------------------

health_checks() {
    log STEP "Running health checks"

    local failed=false

    if $DRY_RUN; then
        log INFO "Would run health checks"
        return 0
    fi

    # Check services running
    for service in "${SERVICES_START[@]}"; do
        if systemctl is-active --quiet "$service" 2>/dev/null; then
            log OK "$service is running"
        else
            log ERROR "$service is not running"
            failed=true
        fi
    done

    # Check MCP server registration
    if claude mcp list 2>/dev/null | grep -q "lobster-inbox"; then
        log OK "MCP server registered"
    else
        log WARN "MCP server may not be registered"
    fi

    # Check session ID file
    if [ -f "$WORKSPACE_DIR/.lobster_session_id" ]; then
        log OK "Session ID file exists"
    else
        log WARN "Session ID file missing (new session will be created)"
    fi

    # Check config file
    if [ -f "$CONFIG_FILE" ]; then
        if grep -q "TELEGRAM_BOT_TOKEN" "$CONFIG_FILE"; then
            log OK "Config file valid"
        else
            log ERROR "Config file missing bot token"
            failed=true
        fi
    else
        log ERROR "Config file missing"
        failed=true
    fi

    # Check message counts preserved
    if [ -f "$BACKUP_DIR/processed-count.txt" ]; then
        local prev_count=$(cat "$BACKUP_DIR/processed-count.txt")
        local curr_count=$(find "$MESSAGES_DIR/processed" -name "*.json" -type f 2>/dev/null | wc -l)
        if [ "$curr_count" -ge "$prev_count" ]; then
            log OK "Message count preserved ($curr_count >= $prev_count)"
        else
            log ERROR "Message count decreased ($curr_count < $prev_count)"
            failed=true
        fi
    fi

    # Optional: Telegram API check
    if [ -f "$CONFIG_FILE" ]; then
        source "$CONFIG_FILE"
        if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
            if curl -s --connect-timeout 5 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null | grep -q '"ok":true'; then
                log OK "Telegram API reachable"
            else
                log WARN "Telegram API check failed (may be transient)"
            fi
        fi
    fi

    if $failed; then
        return 1
    fi
    return 0
}

#-------------------------------------------------------------------------------
# Rollback
#-------------------------------------------------------------------------------

perform_rollback() {
    log STEP "Performing rollback"

    # Find most recent backup
    local latest_backup=$(ls -td "$BACKUP_BASE"/backup-* 2>/dev/null | head -1)

    if [ -z "$latest_backup" ] || [ ! -d "$latest_backup" ]; then
        die "No backup found to rollback to" 1
    fi

    log INFO "Rolling back to: $latest_backup"

    if $DRY_RUN; then
        log INFO "Would restore from backup"
        return 0
    fi

    # Stop services
    stop_services

    # Restore git commit
    if [ -f "$latest_backup/git-commit.txt" ]; then
        local old_commit=$(cat "$latest_backup/git-commit.txt")
        cd "$LOBSTER_DIR"
        git checkout "$old_commit" --quiet 2>/dev/null || git reset --hard "$old_commit" --quiet
        log OK "Restored git commit: $old_commit"
    fi

    # Restore state files
    for file in "${STATE_FILES[@]}"; do
        local rel_path="${file#$HOME/}"
        local backup_path="$latest_backup/$rel_path"
        if [ -f "$backup_path" ]; then
            mkdir -p "$(dirname "$file")"
            cp "$backup_path" "$file"
            log OK "Restored: $rel_path"
        fi
    done

    # Start services
    start_services

    log OK "Rollback complete"
}

#-------------------------------------------------------------------------------
# Cleanup
#-------------------------------------------------------------------------------

cleanup_old_backups() {
    log STEP "Cleaning up old backups"

    # Keep last 5 backups
    local count=$(ls -d "$BACKUP_BASE"/backup-* 2>/dev/null | wc -l)

    if [ "$count" -gt 5 ]; then
        local to_delete=$((count - 5))

        if $DRY_RUN; then
            log INFO "Would delete $to_delete old backup(s)"
        else
            ls -td "$BACKUP_BASE"/backup-* | tail -n "$to_delete" | while read -r dir; do
                rm -rf "$dir"
                log INFO "Deleted: $(basename "$dir")"
            done
            log OK "Cleanup complete"
        fi
    else
        log OK "No cleanup needed ($count backups)"
    fi
}

#-------------------------------------------------------------------------------
# Notifications
#-------------------------------------------------------------------------------

send_notification() {
    local status="$1"
    local message="$2"

    if [ ! -f "$CONFIG_FILE" ]; then
        return
    fi

    source "$CONFIG_FILE"

    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_ALLOWED_USERS:-}" ]; then
        return
    fi

    # Send to all allowed users
    for user_id in $(echo "$TELEGRAM_ALLOWED_USERS" | tr ',' ' '); do
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=$user_id" \
            -d "text=$message" \
            -d "parse_mode=HTML" \
            >/dev/null 2>&1 || true
    done
}

notify_success() {
    local msg="<b>Lobster updated successfully</b>
Previous: <code>$PREVIOUS_COMMIT</code>
Current:  <code>$CURRENT_COMMIT</code>
All services running normally."

    send_notification "success" "$msg"
}

notify_failure() {
    local reason="$1"
    local msg="<b>Lobster update failed - rolled back</b>
Attempted: <code>${CURRENT_COMMIT:-unknown}</code>
Restored:  <code>$PREVIOUS_COMMIT</code>
Reason: $reason
Check logs: ~/lobster-backups/update-*.log"

    send_notification "failure" "$msg"
}

#-------------------------------------------------------------------------------
# Main
#-------------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force)      FORCE=true ;;
            --skip-claude) SKIP_CLAUDE=true ;;
            --dry-run)    DRY_RUN=true ;;
            --rollback)   ROLLBACK=true ;;
            -h|--help)
                echo "Usage: $0 [--force] [--skip-claude] [--dry-run] [--rollback]"
                echo ""
                echo "Options:"
                echo "  --force        Continue even if health checks fail"
                echo "  --skip-claude  Skip Claude Code CLI update"
                echo "  --dry-run      Show what would happen without making changes"
                echo "  --rollback     Restore from most recent backup"
                exit 0
                ;;
            *)
                die "Unknown option: $1" 1
                ;;
        esac
        shift
    done
}

main() {
    START_TIME=$(date +%s)

    echo -e "${BLUE}${BOLD}"
    echo "═══════════════════════════════════════════════════════════════"
    echo "                    LOBSTER UPDATE"
    echo "═══════════════════════════════════════════════════════════════"
    echo -e "${NC}"

    if $DRY_RUN; then
        log INFO "DRY RUN MODE - no changes will be made"
    fi

    parse_args "$@"
    acquire_lock

    trap cleanup_lock EXIT

    if $ROLLBACK; then
        perform_rollback
        cleanup_lock
        exit 0
    fi

    # Phase 1: Pre-flight checks
    preflight_checks

    # Phase 2: Backup
    create_backup

    # Phase 3: Stop services
    stop_services

    # Phase 4: Git update
    if ! git_update; then
        log ERROR "Git update failed, attempting rollback..."
        if [ -n "$BACKUP_DIR" ]; then
            perform_rollback
            notify_failure "Git merge failed"
        fi
        die "Update failed" 5
    fi

    # Phase 5: Dependencies
    update_dependencies

    # Phase 6: Claude CLI
    update_claude_cli

    # Phase 7: Systemd
    update_systemd

    # Phase 7.5: CLI
    update_cli

    # Phase 8: Start services
    if ! start_services; then
        log ERROR "Services failed to start, attempting rollback..."
        perform_rollback
        notify_failure "Services failed to start"
        die "Update failed" 6
    fi

    # Phase 9: Health checks
    if ! health_checks; then
        if $FORCE; then
            log WARN "Health checks failed but continuing (--force)"
        else
            log ERROR "Health checks failed, attempting rollback..."
            perform_rollback
            notify_failure "Health checks failed"
            die "Update failed" 7
        fi
    fi

    # Phase 10: Cleanup
    cleanup_old_backups

    # Success!
    local elapsed=$(($(date +%s) - START_TIME))

    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "═══════════════════════════════════════════════════════════════"
    echo "                    UPDATE COMPLETE!"
    echo "═══════════════════════════════════════════════════════════════"
    echo -e "${NC}"
    echo ""
    log OK "Update completed in ${elapsed}s"
    log INFO "Previous: $PREVIOUS_COMMIT -> Current: $CURRENT_COMMIT"

    if ! $DRY_RUN; then
        notify_success
    fi

    cleanup_lock
}

# Run main with all arguments
main "$@"
