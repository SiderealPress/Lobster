#!/bin/bash
#===============================================================================
# install-lobster-local.sh -- Install Lobster local sync on macOS
#
# Creates the ~/.lobster directory structure, copies scripts, generates default
# configuration, installs the launchd plist, and optionally sets up macOS
# Keychain for GitHub token storage.
#
# This script is idempotent: safe to run multiple times. Existing config
# files are preserved; scripts and plist are always updated.
#
# Usage:
#   ./install-lobster-local.sh              Interactive install
#   ./install-lobster-local.sh --no-start   Install without starting the service
#   ./install-lobster-local.sh --help       Show help
#===============================================================================

set -euo pipefail

#-------------------------------------------------------------------------------
# Constants
#-------------------------------------------------------------------------------

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LOBSTER_DIR="$HOME/.lobster"
readonly BIN_DIR="$LOBSTER_DIR/bin"
readonly LOG_DIR="$LOBSTER_DIR/logs"
readonly CONFIG_DIR="$LOBSTER_DIR/config"
readonly CONFIG_FILE="$LOBSTER_DIR/sync-config.json"
readonly ENV_FILE="$LOBSTER_DIR/.env"
readonly PLIST_LABEL="com.lobster.sync"
readonly PLIST_TEMPLATE="$SCRIPT_DIR/com.lobster.sync.plist"
readonly PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
readonly KEYCHAIN_SERVICE="lobster-sync"
readonly KEYCHAIN_ACCOUNT="github-token"
readonly CLAUDE_SETTINGS="$HOME/.claude/settings.json"

# Control flags
AUTO_START=true

#-------------------------------------------------------------------------------
# Terminal colors (only when stdout is a terminal)
#-------------------------------------------------------------------------------

if [[ -t 1 ]]; then
    readonly C_GREEN=$'\033[32m'
    readonly C_RED=$'\033[31m'
    readonly C_YELLOW=$'\033[33m'
    readonly C_DIM=$'\033[2m'
    readonly C_BOLD=$'\033[1m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_GREEN="" C_RED="" C_YELLOW="" C_DIM="" C_BOLD="" C_RESET=""
fi

#-------------------------------------------------------------------------------
# Helper functions
#-------------------------------------------------------------------------------

info() {
    printf '  %s-->%s %s\n' "$C_BOLD" "$C_RESET" "$*"
}

success() {
    printf '  %s[ok]%s %s\n' "$C_GREEN" "$C_RESET" "$*"
}

warn() {
    printf '  %s[!!]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2
}

die() {
    printf '  %s[FAIL]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
    exit 1
}

# Ask a yes/no question, default to the given value.
# Returns 0 for yes, 1 for no.
ask_yn() {
    local prompt="$1" default="${2:-y}"
    local yn_hint="[Y/n]"
    [[ "$default" == "n" ]] && yn_hint="[y/N]"

    if [[ ! -t 0 ]]; then
        # Non-interactive: use default
        [[ "$default" == "y" ]]
        return $?
    fi

    local reply
    printf '  %s%s%s %s ' "$C_BOLD" "$prompt" "$C_RESET" "$yn_hint"
    read -r reply
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

#-------------------------------------------------------------------------------
# Step 1: Create directory structure
#-------------------------------------------------------------------------------

step_directories() {
    printf '\n%s[1/6] Directory structure%s\n' "$C_BOLD" "$C_RESET"

    local dirs=("$LOBSTER_DIR" "$BIN_DIR" "$LOG_DIR" "$CONFIG_DIR")
    for dir in "${dirs[@]}"; do
        if [[ -d "$dir" ]]; then
            success "$dir (exists)"
        else
            mkdir -p "$dir"
            success "$dir (created)"
        fi
    done
}

#-------------------------------------------------------------------------------
# Step 2: Copy scripts
#-------------------------------------------------------------------------------

step_scripts() {
    printf '\n%s[2/6] Install scripts%s\n' "$C_BOLD" "$C_RESET"

    # Copy daemon and repo sync scripts
    local daemon_src="$REPO_ROOT/scripts/lobster-sync-daemon.sh"
    local repo_src="$REPO_ROOT/scripts/lobster-sync-repo.sh"
    local cli_src="$SCRIPT_DIR/lobster-sync"

    [[ -f "$daemon_src" ]] || die "Daemon script not found: $daemon_src"
    [[ -f "$repo_src" ]]   || die "Repo sync script not found: $repo_src"
    [[ -f "$cli_src" ]]    || die "CLI wrapper not found: $cli_src"

    cp "$daemon_src" "$BIN_DIR/lobster-sync-daemon.sh"
    chmod +x "$BIN_DIR/lobster-sync-daemon.sh"
    success "lobster-sync-daemon.sh -> $BIN_DIR/"

    cp "$repo_src" "$BIN_DIR/lobster-sync-repo.sh"
    chmod +x "$BIN_DIR/lobster-sync-repo.sh"
    success "lobster-sync-repo.sh -> $BIN_DIR/"

    cp "$cli_src" "$BIN_DIR/lobster-sync"
    chmod +x "$BIN_DIR/lobster-sync"
    success "lobster-sync -> $BIN_DIR/"

    # Symlink CLI to /usr/local/bin if possible
    local symlink_target="/usr/local/bin/lobster-sync"
    if [[ -w "/usr/local/bin" ]] || [[ -w "$(dirname "$symlink_target")" ]]; then
        ln -sf "$BIN_DIR/lobster-sync" "$symlink_target"
        success "Symlinked lobster-sync to $symlink_target"
    elif [[ -L "$symlink_target" ]] || [[ -f "$symlink_target" ]]; then
        info "lobster-sync already in PATH at $symlink_target"
    else
        warn "Cannot write to /usr/local/bin. Add $BIN_DIR to your PATH:"
        warn "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc"
    fi
}

#-------------------------------------------------------------------------------
# Step 3: Generate default config
#-------------------------------------------------------------------------------

step_config() {
    printf '\n%s[3/6] Configuration%s\n' "$C_BOLD" "$C_RESET"

    if [[ -f "$CONFIG_FILE" ]]; then
        success "sync-config.json (exists, preserved)"
    else
        cat > "$CONFIG_FILE" <<'CONFIG'
{
  "sync_interval_seconds": 300,
  "sync_branch": "lobster-sync",
  "repos": []
}
CONFIG
        success "sync-config.json (created with defaults)"
        info "Add repos with: lobster-sync add ~/lobster-workspace/projects/my-repo"
    fi
}

#-------------------------------------------------------------------------------
# Step 4: Install launchd plist
#-------------------------------------------------------------------------------

step_plist() {
    printf '\n%s[4/6] launchd service%s\n' "$C_BOLD" "$C_RESET"

    [[ -f "$PLIST_TEMPLATE" ]] || die "Plist template not found: $PLIST_TEMPLATE"

    # Ensure LaunchAgents directory exists
    mkdir -p "$HOME/Library/LaunchAgents"

    # Unload existing service if loaded (so we can update the plist)
    if launchctl list "$PLIST_LABEL" > /dev/null 2>&1; then
        launchctl unload "$PLIST_DEST" 2>/dev/null || true
        info "Unloaded existing service for update"
    fi

    # Generate plist by replacing placeholders with actual home directory
    sed "s|__USER_HOME__|$HOME|g" "$PLIST_TEMPLATE" > "$PLIST_DEST"
    success "Installed plist to $PLIST_DEST"
}

#-------------------------------------------------------------------------------
# Step 5: Keychain setup (optional)
#-------------------------------------------------------------------------------

step_keychain() {
    printf '\n%s[5/6] GitHub token%s\n' "$C_BOLD" "$C_RESET"

    # Check if token already exists in Keychain
    local existing_token
    existing_token="$(security find-generic-password \
        -s "$KEYCHAIN_SERVICE" \
        -a "$KEYCHAIN_ACCOUNT" \
        -w 2>/dev/null)" && {
        success "GitHub token found in Keychain"
        return
    }

    # Check if token exists in .env file
    if [[ -f "$ENV_FILE" ]] && grep -qE '^GITHUB_TOKEN=.+' "$ENV_FILE" 2>/dev/null; then
        success "GitHub token found in $ENV_FILE"
        if ask_yn "Move token to macOS Keychain (more secure)?" "y"; then
            local token
            token="$(grep -E '^GITHUB_TOKEN=' "$ENV_FILE" | head -1)"
            token="${token#GITHUB_TOKEN=}"
            if [[ -n "$token" ]]; then
                security add-generic-password \
                    -s "$KEYCHAIN_SERVICE" \
                    -a "$KEYCHAIN_ACCOUNT" \
                    -w "$token" \
                    -U 2>/dev/null && {
                    success "Token stored in Keychain"
                    # Comment out the plaintext token in .env
                    sed -i '' 's/^GITHUB_TOKEN=/#GITHUB_TOKEN=/' "$ENV_FILE" 2>/dev/null || true
                    info "Commented out plaintext token in $ENV_FILE"
                    return
                }
            fi
        fi
        return
    fi

    # No token found anywhere -- walk the user through setup
    info "No GitHub token found."
    info "A token with 'repo' scope is needed to push sync branches."
    info ""
    info "Create one at: https://github.com/settings/tokens/new"
    info "Required scope: repo"
    info ""

    if ! ask_yn "Set up GitHub token now?" "y"; then
        warn "Skipping token setup. Sync will work locally but cannot push."
        warn "Run this installer again or add manually:"
        warn "  security add-generic-password -s $KEYCHAIN_SERVICE -a $KEYCHAIN_ACCOUNT -w <token>"
        return
    fi

    if [[ ! -t 0 ]]; then
        warn "Non-interactive mode: cannot read token. Set it manually."
        return
    fi

    printf '  %sGitHub token:%s ' "$C_BOLD" "$C_RESET"
    local token
    read -rs token
    printf '\n'

    if [[ -z "$token" ]]; then
        warn "Empty token, skipping."
        return
    fi

    if ask_yn "Store in macOS Keychain (recommended)?" "y"; then
        security add-generic-password \
            -s "$KEYCHAIN_SERVICE" \
            -a "$KEYCHAIN_ACCOUNT" \
            -w "$token" \
            -U 2>/dev/null && {
            success "Token stored in Keychain"
            return
        }
        warn "Keychain storage failed. Falling back to .env file."
    fi

    # Fall back to .env file
    printf 'GITHUB_TOKEN=%s\n' "$token" >> "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    success "Token saved to $ENV_FILE"
}

#-------------------------------------------------------------------------------
# Step 6: Claude Code settings (optional)
#-------------------------------------------------------------------------------

step_claude_settings() {
    printf '\n%s[6/6] Claude Code settings (optional)%s\n' "$C_BOLD" "$C_RESET"

    if ! ask_yn "Configure Claude Code MCP settings for the local bridge?" "n"; then
        info "Skipped. You can configure this later."
        return
    fi

    if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
        mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
        printf '{}\n' > "$CLAUDE_SETTINGS"
        info "Created $CLAUDE_SETTINGS"
    fi

    # Check if jq is available for JSON manipulation
    if ! command -v jq > /dev/null 2>&1; then
        warn "jq not found. Please manually configure $CLAUDE_SETTINGS"
        return
    fi

    # Add lobster-bridge MCP server config if not already present
    local has_lobster
    has_lobster="$(jq -r '.mcpServers["lobster-bridge"] // empty' "$CLAUDE_SETTINGS" 2>/dev/null)" || true
    if [[ -n "$has_lobster" ]]; then
        success "lobster-bridge already configured in Claude settings"
        return
    fi

    local tmp
    tmp="$(jq '. + {"mcpServers": (.mcpServers // {} | . + {"lobster-bridge": {"type": "stdio", "command": "'"$BIN_DIR"'/lobster-bridge", "args": []}})}' "$CLAUDE_SETTINGS")"
    printf '%s\n' "$tmp" > "$CLAUDE_SETTINGS"
    success "Added lobster-bridge to Claude Code settings"
    info "Note: The bridge script (Layer 3) must also be installed for this to work."
}

#-------------------------------------------------------------------------------
# Post-install: optionally start the service
#-------------------------------------------------------------------------------

step_start() {
    printf '\n'
    if $AUTO_START && ask_yn "Start the sync service now?" "y"; then
        launchctl load "$PLIST_DEST"
        success "Service started"
        info "Check status with: lobster-sync status"
    else
        info "Service installed but not started."
        info "Start with: lobster-sync start"
    fi
}

#-------------------------------------------------------------------------------
# Summary
#-------------------------------------------------------------------------------

print_summary() {
    printf '\n%s========================================%s\n' "$C_BOLD" "$C_RESET"
    printf '%s  Lobster Sync installed successfully!%s\n' "$C_GREEN" "$C_RESET"
    printf '%s========================================%s\n' "$C_BOLD" "$C_RESET"
    printf '\n'
    printf '  Home:     %s\n' "$LOBSTER_DIR"
    printf '  Config:   %s\n' "$CONFIG_FILE"
    printf '  Logs:     %s\n' "$LOG_DIR"
    printf '  Service:  %s\n' "$PLIST_DEST"
    printf '\n'
    printf '  Next steps:\n'
    printf '    1. Add repos:  lobster-sync add ~/lobster-workspace/projects/my-repo\n'
    printf '    2. Check:      lobster-sync status\n'
    printf '    3. View logs:  lobster-sync log\n'
    printf '\n'
}

#-------------------------------------------------------------------------------
# Prerequisite checks
#-------------------------------------------------------------------------------

check_prerequisites() {
    printf '%s[0/6] Prerequisites%s\n' "$C_BOLD" "$C_RESET"

    # Must be macOS
    [[ "$(uname)" == "Darwin" ]] || die "This installer is for macOS only."
    success "macOS detected"

    # git must be available
    command -v git > /dev/null 2>&1 || die "git is required but not found."
    success "git found: $(git --version | head -1)"

    # jq must be available
    if command -v jq > /dev/null 2>&1; then
        success "jq found: $(jq --version)"
    else
        die "jq is required. Install with: brew install jq"
    fi

    # Source scripts must exist
    [[ -f "$REPO_ROOT/scripts/lobster-sync-daemon.sh" ]] || \
        die "Source scripts not found. Run from the Lobster repo: macos/install-lobster-local.sh"
    success "Source scripts found in $REPO_ROOT/scripts/"
}

#-------------------------------------------------------------------------------
# Parse arguments
#-------------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-start)  AUTO_START=false; shift ;;
            --help|-h)
                printf 'Usage: %s [--no-start] [--help]\n' "$(basename "$0")"
                printf '\nInstalls Lobster local sync on macOS.\n'
                printf '\nOptions:\n'
                printf '  --no-start   Install without starting the service\n'
                printf '  --help       Show this help message\n'
                exit 0
                ;;
            *)
                die "Unknown option: $1"
                ;;
        esac
    done
}

#-------------------------------------------------------------------------------
# Main
#-------------------------------------------------------------------------------

main() {
    parse_args "$@"

    printf '\n%s=== Lobster Local Sync Installer ===%s\n\n' "$C_BOLD" "$C_RESET"

    check_prerequisites
    step_directories
    step_scripts
    step_config
    step_plist
    step_keychain
    step_claude_settings
    step_start
    print_summary
}

main "$@"
