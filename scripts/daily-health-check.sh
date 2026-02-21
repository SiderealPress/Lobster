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

INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
INBOX_DIR="$MESSAGES_DIR/inbox"
LOG_FILE="$WORKSPACE_DIR/logs/daily-health-check.log"
TIMESTAMP=$(date -Iseconds)

mkdir -p "$(dirname "$LOG_FILE")" "$INBOX_DIR"

# Ensure PATH includes common tool locations
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | sort -V | tail -1)/bin:$PATH"

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

log "=== Health check complete: ${#FAILURES[@]} failure(s) ==="

#-------------------------------------------------------------------------------
# On failure, write a message to the Lobster inbox so it gets picked up
#-------------------------------------------------------------------------------
if [ ${#FAILURES[@]} -gt 0 ]; then
    FAIL_LIST=$(printf '%s\n' "${FAILURES[@]}" | sed 's/^/  - /')
    MSG_FILE="$INBOX_DIR/daily-health-$(date +%Y%m%d-%H%M%S).json"
    cat > "$MSG_FILE" << MSGEOF
{
  "type": "task-output",
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
