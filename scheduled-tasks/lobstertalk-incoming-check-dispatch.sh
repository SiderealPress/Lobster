#!/bin/bash
# LobsterTalk Incoming Handler Pre-Check Dispatcher
#
# Tests SSH connectivity to the bot-talk host before dispatching the
# lobstertalk-incoming-handler job. If the host is unreachable, logs a
# brief "host unreachable, skipping" message and exits cleanly — no LLM
# subagent is spawned, no error is escalated.
#
# If the host is reachable, delegates to dispatch-job.sh as normal.
#
# Usage: lobstertalk-incoming-check-dispatch.sh lobstertalk-incoming-handler
#
# Flow:
#   host reachable   → dispatch-job.sh lobstertalk-incoming-handler → inbox write → LLM
#   host unreachable → log skip → exit 0 (no inbox write)

set -e

export PATH="$HOME/.local/bin:$PATH"

JOB_NAME="${1:-lobstertalk-incoming-handler}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load env files (same pattern as dispatch-job.sh)
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
for _env_file in "$CONFIG_DIR/config.env" "$CONFIG_DIR/global.env"; do
    if [ -f "$_env_file" ]; then
        # shellcheck source=/dev/null
        set -a
        source "$_env_file"
        set +a
    fi
done
unset _env_file

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOG_DIR="$WORKSPACE/scheduled-jobs/logs"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
START_ISO=$(date -Iseconds)

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${JOB_NAME}-precheck-${TIMESTAMP}.log"

# --- SSH connectivity pre-check ---
# The bot-talk host. Can be overridden via BOT_TALK_HOST in config.env.
BOT_TALK_HOST="${BOT_TALK_HOST:-46.224.41.108}"
BOT_TALK_USER="${BOT_TALK_USER:-shared}"

# Test SSH connectivity with a short timeout. BatchMode=yes prevents interactive
# prompts; StrictHostKeyChecking=no avoids blocking on first-connect key checks.
# We only test connectivity (ssh ... exit) — no actual work is performed here.
if ! ssh \
    -o ConnectTimeout=5 \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    "${BOT_TALK_USER}@${BOT_TALK_HOST}" exit 2>/dev/null; then
    echo "[$START_ISO] Host ${BOT_TALK_HOST} unreachable — skipping dispatch for $JOB_NAME" | tee "$LOG_FILE"
    exit 0
fi

# Host is reachable — delegate to standard dispatcher
echo "[$START_ISO] Host ${BOT_TALK_HOST} reachable — dispatching $JOB_NAME" | tee "$LOG_FILE"
exec "$SCRIPT_DIR/dispatch-job.sh" "$JOB_NAME"
