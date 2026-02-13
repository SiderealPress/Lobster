#!/bin/bash
#===============================================================================
# uninstall-lobster-local.sh -- Remove Lobster local sync from macOS
#
# Unloads the launchd service, removes the plist, and optionally removes the
# ~/.lobster directory. Does NOT remove git repos or sync branches.
#
# Usage:
#   ./uninstall-lobster-local.sh           Interactive uninstall
#   ./uninstall-lobster-local.sh --full    Remove everything including ~/.lobster
#   ./uninstall-lobster-local.sh --help    Show help
#===============================================================================

set -euo pipefail

#-------------------------------------------------------------------------------
# Constants
#-------------------------------------------------------------------------------

readonly LOBSTER_DIR="$HOME/.lobster"
readonly PLIST_LABEL="com.lobster.sync"
readonly PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
readonly BIN_DIR="$LOBSTER_DIR/bin"
readonly SYMLINK_PATH="/usr/local/bin/lobster-sync"
readonly KEYCHAIN_SERVICE="lobster-sync"
readonly KEYCHAIN_ACCOUNT="github-token"

# Control flags
FULL_REMOVE=false

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

ask_yn() {
    local prompt="$1" default="${2:-n}"
    local yn_hint="[y/N]"
    [[ "$default" == "y" ]] && yn_hint="[Y/n]"

    if [[ ! -t 0 ]]; then
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
# Step 1: Stop and unload the launchd service
#-------------------------------------------------------------------------------

step_service() {
    printf '\n%s[1/4] launchd service%s\n' "$C_BOLD" "$C_RESET"

    if launchctl list "$PLIST_LABEL" > /dev/null 2>&1; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        success "Service unloaded"
    else
        info "Service was not loaded"
    fi

    # Remove plist file
    if [[ -f "$PLIST_PATH" ]]; then
        rm -f "$PLIST_PATH"
        success "Removed $PLIST_PATH"
    else
        info "Plist not found (already removed)"
    fi

    # Clean up PID file
    local pid_file="$LOBSTER_DIR/sync.pid"
    if [[ -f "$pid_file" ]]; then
        local pid
        pid="$(cat "$pid_file")"
        if kill -0 "$pid" 2>/dev/null; then
            info "Stopping daemon process (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 2
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
        success "Removed PID file"
    fi
}

#-------------------------------------------------------------------------------
# Step 2: Remove symlinks
#-------------------------------------------------------------------------------

step_symlinks() {
    printf '\n%s[2/4] CLI symlinks%s\n' "$C_BOLD" "$C_RESET"

    if [[ -L "$SYMLINK_PATH" ]]; then
        rm -f "$SYMLINK_PATH"
        success "Removed $SYMLINK_PATH"
    elif [[ -f "$SYMLINK_PATH" ]]; then
        warn "$SYMLINK_PATH exists but is not a symlink. Leaving it."
    else
        info "No symlink at $SYMLINK_PATH"
    fi
}

#-------------------------------------------------------------------------------
# Step 3: Optionally remove Keychain entry
#-------------------------------------------------------------------------------

step_keychain() {
    printf '\n%s[3/4] Keychain%s\n' "$C_BOLD" "$C_RESET"

    # Check if Keychain entry exists
    if security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" > /dev/null 2>&1; then
        if ask_yn "Remove GitHub token from Keychain?" "n"; then
            security delete-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" 2>/dev/null || true
            success "Removed Keychain entry"
        else
            info "Keychain entry preserved"
        fi
    else
        info "No Keychain entry found"
    fi
}

#-------------------------------------------------------------------------------
# Step 4: Optionally remove ~/.lobster directory
#-------------------------------------------------------------------------------

step_directory() {
    printf '\n%s[4/4] Data directory%s\n' "$C_BOLD" "$C_RESET"

    if [[ ! -d "$LOBSTER_DIR" ]]; then
        info "$LOBSTER_DIR does not exist"
        return
    fi

    if $FULL_REMOVE || ask_yn "Remove $LOBSTER_DIR? (config, logs, scripts will be deleted)" "n"; then
        rm -rf "$LOBSTER_DIR"
        success "Removed $LOBSTER_DIR"
    else
        info "$LOBSTER_DIR preserved"
        info "You can remove it later with: rm -rf $LOBSTER_DIR"
    fi
}

#-------------------------------------------------------------------------------
# Summary
#-------------------------------------------------------------------------------

print_summary() {
    printf '\n%s========================================%s\n' "$C_BOLD" "$C_RESET"
    printf '%s  Lobster Sync uninstalled%s\n' "$C_GREEN" "$C_RESET"
    printf '%s========================================%s\n' "$C_BOLD" "$C_RESET"
    printf '\n'
    printf '  Note: Git repos and lobster-sync branches were NOT removed.\n'
    printf '  To remove sync branches from a repo:\n'
    printf '    git branch -D lobster-sync\n'
    printf '    git push origin --delete lobster-sync\n'
    printf '\n'
}

#-------------------------------------------------------------------------------
# Parse arguments
#-------------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --full)  FULL_REMOVE=true; shift ;;
            --help|-h)
                printf 'Usage: %s [--full] [--help]\n' "$(basename "$0")"
                printf '\nUninstalls Lobster local sync from macOS.\n'
                printf '\nOptions:\n'
                printf '  --full    Remove everything including ~/.lobster directory\n'
                printf '  --help    Show this help message\n'
                printf '\nDoes NOT remove:\n'
                printf '  - Git repositories\n'
                printf '  - lobster-sync branches in repos\n'
                printf '  - Remote sync branches on GitHub\n'
                exit 0
                ;;
            *)
                printf 'Unknown option: %s\n' "$1" >&2
                exit 1
                ;;
        esac
    done
}

#-------------------------------------------------------------------------------
# Main
#-------------------------------------------------------------------------------

main() {
    parse_args "$@"

    printf '\n%s=== Lobster Sync Uninstaller ===%s\n' "$C_BOLD" "$C_RESET"

    step_service
    step_symlinks
    step_keychain
    step_directory
    print_summary
}

main "$@"
