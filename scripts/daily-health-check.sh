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
    _DEV_MODE=$(grep -m1 '^LOBSTER_DEV_MODE=' "$_LOBSTER_CONFIG" 2>/dev/null | cut -d= -f2 || true)
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

# NOTE: OS package upgrades are intentionally NOT performed here.
# Running apt-get upgrade in the health check caused needrestart to fire
# SIGTERM at all lobster Python services without going through restart-mcp.sh,
# which left the dispatcher with no warning before MCP session invalidation.
# Upgrades now live in scripts/run-upgrades.sh, which calls restart-mcp.sh
# first so the dispatcher gets a warning and the MCP restarts cleanly.
# See issue #1757.

log "=== Health check complete: ${#FAILURES[@]} failure(s) ==="

#-------------------------------------------------------------------------------
# On failure, write a message to the Lobster inbox so it gets picked up
#-------------------------------------------------------------------------------
if [ ${#FAILURES[@]} -gt 0 ]; then
    # Build the inbox message using jq so that embedded newlines in $FAIL_LIST are
    # properly escaped as \n in the JSON string.  A heredoc with bare variable
    # expansion produces literal newlines inside a JSON string value, which makes
    # json.load() throw "Invalid control character" — causing WFM to tight-loop
    # and exhaust --max-turns (the 2026-04-24 / 2026-04-25 restart storm bug).
    BODY="The daily dependency health check found problems:"$'\n\n'"$(printf '%s\n' "${FAILURES[@]}" | sed 's/^/  - /')"$'\n\n'"Check the log for details: $LOG_FILE"
    MSG_ID="daily-health-$(date +%Y%m%d-%H%M%S)"
    MSG_FILE="$INBOX_DIR/${MSG_ID}.json"
    jq -n \
        --arg id        "$MSG_ID" \
        --arg type      "health_check" \
        --arg source    "system" \
        --arg timestamp "$TIMESTAMP" \
        --arg subject   "Daily health check: ${#FAILURES[@]} failure(s)" \
        --arg body      "$BODY" \
        --arg severity  "warning" \
        '{id: $id, type: $type, source: $source, timestamp: $timestamp, subject: $subject, body: $body, severity: $severity}' \
        > "$MSG_FILE"
    log "Failure alert written to inbox: $MSG_FILE"
    exit 1
fi

exit 0
