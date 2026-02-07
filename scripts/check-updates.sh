#!/bin/bash
#===============================================================================
# Lobster Update Checker
#
# Checks for available updates and notifies via Telegram.
# Designed to be run as a scheduled job - does NOT apply updates automatically.
#
# Usage: ./check-updates.sh
#
# Returns:
#   - JSON output suitable for scheduled task processing
#   - Sends Telegram notification if updates are available
#===============================================================================

set -euo pipefail

LOBSTER_DIR="$HOME/lobster"
CONFIG_FILE="$LOBSTER_DIR/config/config.env"

# Change to repo directory
cd "$LOBSTER_DIR" || {
    echo '{"status": "error", "message": "Lobster directory not found"}'
    exit 1
}

# Fetch latest from origin
if ! git fetch origin main --quiet 2>/dev/null; then
    echo '{"status": "error", "message": "Failed to fetch from origin"}'
    exit 1
fi

# Get current and remote commits
CURRENT_COMMIT=$(git rev-parse --short HEAD)
REMOTE_COMMIT=$(git rev-parse --short origin/main)
COMMITS_BEHIND=$(git rev-list HEAD..origin/main --count 2>/dev/null || echo "0")

# No updates available
if [ "$COMMITS_BEHIND" = "0" ]; then
    echo '{"status": "up_to_date", "current_commit": "'"$CURRENT_COMMIT"'", "commits_behind": 0}'
    exit 0
fi

# Get commit summaries
COMMIT_LOG=$(git log --oneline HEAD..origin/main 2>/dev/null | head -5)
SUMMARY=$(echo "$COMMIT_LOG" | head -3 | tr '\n' '; ' | sed 's/; $//')

# Output JSON
cat << EOF
{
  "status": "updates_available",
  "current_commit": "$CURRENT_COMMIT",
  "latest_commit": "$REMOTE_COMMIT",
  "commits_behind": $COMMITS_BEHIND,
  "summary": "$SUMMARY"
}
EOF

# Send Telegram notification if config exists
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"

    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_ALLOWED_USERS:-}" ]; then
        MESSAGE="<b>Lobster Update Available</b>

<code>$COMMITS_BEHIND</code> new commit(s) available

Current: <code>$CURRENT_COMMIT</code>
Latest:  <code>$REMOTE_COMMIT</code>

Recent changes:
<pre>$(echo "$COMMIT_LOG" | head -5)</pre>

Run <code>lobster update</code> to apply."

        for user_id in $(echo "$TELEGRAM_ALLOWED_USERS" | tr ',' ' '); do
            curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                -d "chat_id=$user_id" \
                -d "text=$MESSAGE" \
                -d "parse_mode=HTML" \
                >/dev/null 2>&1 || true
        done
    fi
fi
