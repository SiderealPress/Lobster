#!/bin/bash
# awp-poll-calendar.sh — Poll AWP calendar every 15 minutes (BIS-133)
#
# Calls the AWP /api/cron/poll-calendar endpoint to detect new investor meetings.
# This is the Lobster-side fallback for the Vercel Cron trigger, ensuring the
# pipeline runs even if Vercel silently fails.
#
# Required config (in ~/lobster-config/config.env):
#   AWP_BASE_URL=<your AWP Vercel URL>
#   AWP_CRON_SECRET=<value of CRON_SECRET from Vercel environment>
#
# Optional: AWP_CRON_SECRET may be omitted if the Vercel endpoint allows
# unauthenticated access (i.e. CRON_SECRET is not set on the Vercel project).
#
# Schedule: every 15 minutes (managed by Lobster systemd timer)
# Logs: ~/lobster-workspace/scheduled-jobs/logs/awp-poll-calendar-*.log

set -euo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# ── Load env ───────────────────────────────────────────────────────────────────

CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
for _env_file in "$CONFIG_DIR/config.env" "$CONFIG_DIR/global.env"; do
    if [ -f "$_env_file" ]; then
        set -a
        # shellcheck source=/dev/null
        source "$_env_file"
        set +a
    fi
done

# ── Validate required env ──────────────────────────────────────────────────────

AWP_BASE_URL="${AWP_BASE_URL:-}"
if [ -z "$AWP_BASE_URL" ]; then
    echo "[awp-poll-calendar] ERROR: AWP_BASE_URL is not set in config.env" >&2
    exit 1
fi

# ── Build request ──────────────────────────────────────────────────────────────

ENDPOINT="${AWP_BASE_URL}/api/cron/poll-calendar"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "[awp-poll-calendar] $TIMESTAMP — Polling $ENDPOINT"

# Include CRON_SECRET auth header if configured
AUTH_ARGS=()
if [ -n "${AWP_CRON_SECRET:-}" ]; then
    AUTH_ARGS+=(--header "Authorization: Bearer $AWP_CRON_SECRET")
fi

# ── Call endpoint ──────────────────────────────────────────────────────────────

HTTP_RESPONSE=$(
    curl \
        --silent \
        --show-error \
        --max-time 30 \
        --write-out "\n%{http_code}" \
        "${AUTH_ARGS[@]}" \
        --header "User-Agent: Lobster/awp-poll-calendar" \
        --get \
        "$ENDPOINT" \
    2>&1
)

# Split body and HTTP status code
HTTP_STATUS=$(echo "$HTTP_RESPONSE" | tail -n1)
HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)

echo "[awp-poll-calendar] Status: $HTTP_STATUS"
echo "[awp-poll-calendar] Response: $HTTP_BODY"

# ── Check result ───────────────────────────────────────────────────────────────

if [ "$HTTP_STATUS" -ne 200 ]; then
    echo "[awp-poll-calendar] ERROR: Non-200 response ($HTTP_STATUS)" >&2
    exit 1
fi

echo "[awp-poll-calendar] Done."
