#!/usr/bin/env bash
# claude-process-monitor.sh — alert if too many `claude -p` processes are running.
#
# Installed by install.sh when LOBSTER_DEBUG=true.
# Runs via cron every 2 minutes: */2 * * * * ... # LOBSTER-CLAUDE-P-MONITOR
#
# Thresholds:
#   MAX_CLAUDE_P=1   (alert if > 1 running simultaneously)
#
# Alert is sent via Telegram to the owner chat ID.

set -euo pipefail

CONFIG_ENV="${HOME}/lobster-config/config.env"
MAX_CLAUDE_P="${MAX_CLAUDE_P:-1}"

# Load config
if [[ -f "$CONFIG_ENV" ]]; then
    # shellcheck disable=SC1090
    source <(grep -E '^[A-Z_]+=.' "$CONFIG_ENV" | sed "s/'//g")
fi

LOBSTER_DEBUG="${LOBSTER_DEBUG:-false}"
if [[ "$LOBSTER_DEBUG" != "true" ]]; then
    exit 0
fi

TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
OWNER_CHAT_ID="${OWNER_CHAT_ID:-8305714125}"

if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
    echo "[claude-process-monitor] ERROR: TELEGRAM_BOT_TOKEN not set" >&2
    exit 1
fi

# Count running claude -p / claude --print processes
COUNT=$(ps aux | grep -E "claude (-p|--print)" | grep -v grep | wc -l | tr -d ' ')

if [[ "$COUNT" -gt "$MAX_CLAUDE_P" ]]; then
    MSG="⚠️ claude -p overload: ${COUNT} instances running (limit: ${MAX_CLAUDE_P}). Check for runaway cron jobs."
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${OWNER_CHAT_ID}" \
        --data-urlencode "text=${MSG}" \
        -o /dev/null
    echo "[claude-process-monitor] ALERT: ${COUNT} claude -p processes (limit ${MAX_CLAUDE_P})"
fi
