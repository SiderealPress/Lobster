#!/bin/bash
#===============================================================================
# Lobster Daily Dependency Health Check
#
# Tests that each tool and Python dependency Lobster relies on is working.
# Writes to the inbox ONLY on failure - silent on success.
#
# Run via cron at 06:00 daily:
#   0 6 * * * /home/.../lobster/scripts/daily-health-check.sh # LOBSTER-DAILY-HEALTH
#===============================================================================

set -o pipefail

# Developer mode: suppress all system notifications so the developer isn't
# bothered while testing. Real user messages are never affected by this flag.
_LOBSTER_CONFIG="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"
if [ -f "$_LOBSTER_CONFIG" ]; then
    _DEV_MODE=$(grep -m1 '^LOBSTER_DEV_MODE=' "$_LOBSTER_CONFIG" 2>/dev/null | cut -d= -f2)
    if [ "$_DEV_MODE" = "true" ] || [ "$_DEV_MODE" = "1" ]; then
        exit 0
    fi
fi
unset _LOBSTER_CONFIG _DEV_MODE

INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
INBOX_DIR="$MESSAGES_DIR/inbox"
LOG_FILE="$WORKSPACE_DIR/logs/daily-health-check.log"
TIMESTAMP=$(date -Iseconds)

mkdir -p "$(dirname "$LOG_FILE")" "$INBOX_DIR"

# Ensure PATH includes common tool locations
export PATH="$HOME/.local/bin:/usr/local/bin:$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | sort -V | tail -1)/bin:$PATH"

FAILURES=()

log() { echo "[$TIMESTAMP] $*" >> "$LOG_FILE"; }

check() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" &>/dev/null; then
        log "OK: $name"
    else
        log "FAIL: $name"
        FAILURES+=("$name")
    fi
}

log "=== Daily health check starting ==="

#-------------------------------------------------------------------------------
# System tools
#-------------------------------------------------------------------------------
check "python3"           "command -v python3"
check "pip"               "command -v pip || command -v pip3"
check "git"               "command -v git"
check "jq"                "command -v jq"
check "curl"              "command -v curl"
check "tmux"              "command -v tmux"
check "crontab"           "command -v crontab"
check "rg (ripgrep)"      "command -v rg"
check "fd"                "command -v fd || command -v fdfind"
check "bat"               "command -v bat || command -v batcat"
check "fzf"               "command -v fzf"
check "claude"            "command -v claude"

#-------------------------------------------------------------------------------
# Python packages (tested inside the venv)
#-------------------------------------------------------------------------------
VENV_PYTHON="$INSTALL_DIR/.venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    check "mcp (python)"          "$VENV_PYTHON -c 'import mcp'"
    check "dotenv (python)"       "$VENV_PYTHON -c 'import dotenv'"
    check "psutil (python)"       "$VENV_PYTHON -c 'import psutil'"
    check "fastembed (python)"    "$VENV_PYTHON -c 'import fastembed'"
    check "sqlite_vec (python)"   "$VENV_PYTHON -c 'import sqlite_vec'"
else
    log "FAIL: venv not found at $VENV_PYTHON"
    FAILURES+=("python-venv")
fi

#-------------------------------------------------------------------------------
# whisper.cpp binary
#-------------------------------------------------------------------------------
WHISPER_CLI="$WORKSPACE_DIR/whisper.cpp/build/bin/whisper-cli"
check "whisper-cli binary"   "[ -x '$WHISPER_CLI' ]"
check "whisper small model"  "[ -f '$WORKSPACE_DIR/whisper.cpp/models/ggml-small.bin' ]"

#-------------------------------------------------------------------------------
# Lobster services
#-------------------------------------------------------------------------------
check "lobster-router (systemd)"  "systemctl is-active --quiet lobster-router"
check "lobster-claude (tmux)"     "tmux -L lobster has-session -t lobster"

#-------------------------------------------------------------------------------
# Inbox directory writable
#-------------------------------------------------------------------------------
check "inbox writable"  "[ -d '$INBOX_DIR' ] && touch '$INBOX_DIR/.health-write-test' && rm '$INBOX_DIR/.health-write-test'"

#-------------------------------------------------------------------------------
# OS package updates
#-------------------------------------------------------------------------------
update_system_packages() {
    local sudo_prefix=""
    if [ "$(id -u)" -ne 0 ]; then
        sudo_prefix="sudo "
    fi

    if command -v apt-get &>/dev/null; then
        log "INFO: update_system_packages: using apt-get"
        # Run apt-get update and capture output so we can inspect it for GPG
        # key errors before deciding whether to fail the health check.
        local apt_update_out
        apt_update_out=$(${sudo_prefix}apt-get update -q 2>&1 | tee -a "$LOG_FILE")
        local apt_update_rc=${PIPESTATUS[0]}

        # Detect untrusted GPG key errors (NO_PUBKEY / not signed).
        # A stale or corrupt third-party keyring (e.g. cli.github.com) causes
        # apt-get update to exit non-zero but does not mean the system is
        # unhealthy — it just means one repo's key needs refreshing.
        # Strategy:
        #   1. If NO_PUBKEY is reported for the GitHub CLI repo, attempt to
        #      refresh the keyring automatically and retry apt-get update.
        #   2. If update still fails only due to GPG errors (not package
        #      conflicts or network outages), log a warning but do not fail the
        #      health check — GPG issues are a configuration matter, not a
        #      system outage.
        #   3. If update fails for non-GPG reasons, fail as usual.
        if echo "$apt_update_out" | grep -q "NO_PUBKEY"; then
            log "WARN: apt-get update reported untrusted GPG key(s) — checking for known fixable repos"
            # Self-heal: refresh GitHub CLI keyring if that specific repo is affected
            if echo "$apt_update_out" | grep -q "cli.github.com"; then
                log "INFO: Attempting to refresh GitHub CLI apt keyring..."
                local keyring_path="/etc/apt/keyrings/githubcli-archive-keyring.gpg"
                if ${sudo_prefix}curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                       | ${sudo_prefix}dd of="$keyring_path" 2>/dev/null; then
                    log "INFO: GitHub CLI keyring refreshed — retrying apt-get update"
                    apt_update_out=$(${sudo_prefix}apt-get update -q 2>&1 | tee -a "$LOG_FILE")
                    apt_update_rc=${PIPESTATUS[0]}
                else
                    log "WARN: Could not refresh GitHub CLI keyring (no network or permission issue)"
                fi
            fi
        fi

        if [ $apt_update_rc -ne 0 ]; then
            # Check if remaining failures are ALL GPG-related (NO_PUBKEY / not signed).
            # If so, treat as a warning — apt-get upgrade can still update packages
            # from repos whose keys ARE trusted. Extract only E: lines, then check
            # whether any of them are NOT about GPG key trust issues.
            local non_gpg_errors
            non_gpg_errors=$(echo "$apt_update_out" | grep "^E:" | grep -vE "NO_PUBKEY|not signed" || true)
            if [ -z "$non_gpg_errors" ]; then
                log "WARN: apt-get update has GPG key warnings only — proceeding with upgrade for trusted repos"
                apt_update_rc=0
            else
                log "ERROR: apt-get update failed (non-GPG error): $non_gpg_errors"
                FAILURES+=("system-packages-apt-get-update")
            fi
        fi

        if [ $apt_update_rc -eq 0 ]; then
            if ${sudo_prefix}apt-get upgrade -y -q &>>"$LOG_FILE"; then
                log "OK: system packages updated (apt-get)"
            else
                log "ERROR: apt-get upgrade failed"
                FAILURES+=("system-packages-apt-get")
            fi
        fi
    elif command -v dnf &>/dev/null; then
        log "INFO: update_system_packages: using dnf"
        if ${sudo_prefix}dnf upgrade -y -q &>>"$LOG_FILE"; then
            log "OK: system packages updated (dnf)"
        else
            log "ERROR: system packages update failed (dnf)"
            FAILURES+=("system-packages-dnf")
        fi
    elif command -v yum &>/dev/null; then
        log "INFO: update_system_packages: using yum"
        if ${sudo_prefix}yum upgrade -y -q &>>"$LOG_FILE"; then
            log "OK: system packages updated (yum)"
        else
            log "ERROR: system packages update failed (yum)"
            FAILURES+=("system-packages-yum")
        fi
    elif command -v pacman &>/dev/null; then
        log "INFO: update_system_packages: using pacman"
        if ${sudo_prefix}pacman -Syu --noconfirm &>>"$LOG_FILE"; then
            log "OK: system packages updated (pacman)"
        else
            log "ERROR: system packages update failed (pacman)"
            FAILURES+=("system-packages-pacman")
        fi
    elif command -v zypper &>/dev/null; then
        log "INFO: update_system_packages: using zypper"
        if ${sudo_prefix}zypper update -y &>>"$LOG_FILE"; then
            log "OK: system packages updated (zypper)"
        else
            log "ERROR: system packages update failed (zypper)"
            FAILURES+=("system-packages-zypper")
        fi
    elif command -v apk &>/dev/null; then
        log "INFO: update_system_packages: using apk"
        if ${sudo_prefix}apk update &>>"$LOG_FILE" && \
           ${sudo_prefix}apk upgrade &>>"$LOG_FILE"; then
            log "OK: system packages updated (apk)"
        else
            log "ERROR: system packages update failed (apk)"
            FAILURES+=("system-packages-apk")
        fi
    else
        log "WARN: update_system_packages: no supported package manager found, skipping"
    fi
}

update_system_packages

log "=== Health check complete: ${#FAILURES[@]} failure(s) ==="

#-------------------------------------------------------------------------------
# On failure, write a message to the Lobster inbox so it gets picked up
#-------------------------------------------------------------------------------
if [ ${#FAILURES[@]} -gt 0 ]; then
    FAIL_LIST=$(printf '%s\n' "${FAILURES[@]}" | sed 's/^/  - /')
    MSG_FILE="$INBOX_DIR/daily-health-$(date +%Y%m%d-%H%M%S).json"
    cat > "$MSG_FILE" << MSGEOF
{
  "type": "health_check",
  "source": "daily-health-check",
  "timestamp": "$TIMESTAMP",
  "subject": "Daily health check: ${#FAILURES[@]} failure(s)",
  "body": "The daily dependency health check found problems:\n\n$FAIL_LIST\n\nCheck the log for details: $LOG_FILE",
  "severity": "warning"
}
MSGEOF
    log "Failure alert written to inbox: $MSG_FILE"
    exit 1
fi

exit 0
