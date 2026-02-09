#!/bin/bash
#
# Claude wrapper for headless/API-key deployments
#
# Uses claude --print in a polling loop instead of interactive mode,
# which requires OAuth. This wrapper works with ANTHROPIC_API_KEY set
# in the environment or in config.env.
#
# For desktop/OAuth setups, use claude-wrapper.exp instead.
#

set -euo pipefail

WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
INSTALL_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
CONFIG_ENV="$INSTALL_DIR/config/config.env"

# Source config.env if it exists (for ANTHROPIC_API_KEY, etc.)
if [ -f "$CONFIG_ENV" ]; then
    set -a
    source "$CONFIG_ENV"
    set +a
fi

# Ensure Claude is in PATH
export PATH="$HOME/.local/bin:$PATH"

# Verify claude is available
if ! command -v claude &>/dev/null; then
    echo "ERROR: claude not found in PATH"
    exit 1
fi

# Verify API key is set
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not set. Cannot run in headless mode without it."
    echo "Set it in $CONFIG_ENV or export it in the environment."
    exit 1
fi

# Read CLAUDE.md for system context
CLAUDE_MD="$WORKSPACE_DIR/CLAUDE.md"
if [ -f "$CLAUDE_MD" ]; then
    SYSTEM_CONTEXT=$(cat "$CLAUDE_MD")
else
    echo "WARNING: $CLAUDE_MD not found, running without system context"
    SYSTEM_CONTEXT=""
fi

POLL_INTERVAL=5
IDLE_POLL_INTERVAL=15
MAX_CONSECUTIVE_EMPTY=12  # After 12 empty polls at fast rate, slow down
consecutive_empty=0

echo "[$(date -Iseconds)] Claude wrapper (headless/print mode) starting"
echo "[$(date -Iseconds)] Workspace: $WORKSPACE_DIR"
echo "[$(date -Iseconds)] Poll interval: ${POLL_INTERVAL}s (idle: ${IDLE_POLL_INTERVAL}s)"

# Main polling loop
while true; do
    # Check if there are messages in inbox
    INBOX_DIR="$HOME/messages/inbox"
    MESSAGE_COUNT=$(find "$INBOX_DIR" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l)

    if [ "$MESSAGE_COUNT" -gt 0 ]; then
        consecutive_empty=0
        echo "[$(date -Iseconds)] Found $MESSAGE_COUNT message(s) in inbox, processing..."

        # Build the prompt
        PROMPT="$SYSTEM_CONTEXT

---

You have messages waiting. Call check_inbox() to see them, then process and respond to each one.
After processing all messages, indicate you are done."

        # Run claude in --print mode with MCP tools
        cd "$WORKSPACE_DIR"
        claude --dangerously-skip-permissions --print \
            --max-turns 25 \
            -p "$PROMPT" \
            2>&1 || {
                echo "[$(date -Iseconds)] Claude exited with code $?, continuing..."
            }

        echo "[$(date -Iseconds)] Processing cycle complete"
    else
        consecutive_empty=$((consecutive_empty + 1))
    fi

    # Adaptive polling: slow down when idle
    if [ "$consecutive_empty" -ge "$MAX_CONSECUTIVE_EMPTY" ]; then
        sleep "$IDLE_POLL_INTERVAL"
    else
        sleep "$POLL_INTERVAL"
    fi
done
