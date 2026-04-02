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
        if ${sudo_prefix}apt-get update -q &>>"$LOG_FILE" && \
           ${sudo_prefix}apt-get upgrade -y -q &>>"$LOG_FILE"; then
            log "OK: system packages updated (apt-get)"
        else
            log "ERROR: system packages update failed (apt-get)"
            FAILURES+=("system-packages-apt-get")
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
