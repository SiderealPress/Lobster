#!/bin/bash
# Nightly Consolidation - Wrapper script for cron
#
# This script runs the Python consolidation process and, if successful,
# injects a summary message into the inbox for the running Claude session.
#
# It can operate in two modes:
#   1. Direct mode (default): Runs the Python script directly (requires
#      ANTHROPIC_API_KEY in environment). Preferred when running standalone.
#   2. Inbox mode (--inject-only): Injects a consolidation message into the
#      inbox queue for the running Claude session to process. Useful when
#      the Python script dependencies are not available.
#
# Crontab entry:
#   0 3 * * * $HOME/lobster/scripts/nightly-consolidation.sh
#
# Environment:
#   CONSOLIDATION_HOUR - Hour to run (default: 3, used by cron setup)
#   ANTHROPIC_API_KEY  - Required for direct mode

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOBSTER_DIR="$(dirname "$SCRIPT_DIR")"
VENV="$LOBSTER_DIR/.venv"
INBOX="$HOME/messages/inbox"
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
CONFIG="$CONFIG_DIR/consolidation.conf"
TIMESTAMP=$(date +%s%3N)

# Ensure inbox directory exists
mkdir -p "$INBOX"

# Parse arguments
INJECT_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --inject-only) INJECT_ONLY=true ;;
    esac
done

if [ "$INJECT_ONLY" = true ]; then
    # Inject a consolidation message for the running Claude session
    cat > "$INBOX/${TIMESTAMP}_consolidation.json" << EOF
{
  "id": "${TIMESTAMP}_consolidation",
  "source": "internal",
  "chat_id": 0,
  "type": "consolidation",
  "text": "NIGHTLY CONSOLIDATION: Review today's events using memory_recent(hours=24) and update canonical memory files. Steps:\n1. Call memory_recent(hours=24) to get all events from the past day\n2. Synthesize key themes, decisions, and action items\n3. Update memory/canonical/daily-digest.md with the synthesis\n4. Update memory/canonical/priorities.md if priorities changed\n5. Update relevant project files in memory/canonical/projects/\n6. Update people files if new relationship info emerged\n7. Mark all reviewed events as consolidated using mark_consolidated\n8. Update memory/canonical/handoff.md with current state",
  "timestamp": "$(date -Iseconds)"
}
EOF
    echo "Consolidation message injected at $(date -Iseconds)"
    exit 0
fi

# Direct mode: run the Python consolidation script
echo "Running nightly consolidation at $(date -Iseconds)"

# Activate virtual environment if available
if [ -f "$VENV/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
fi

# Run the consolidation script
PYTHON="${VENV}/bin/python3"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

"$PYTHON" "$SCRIPT_DIR/nightly-consolidation.py" --config "$CONFIG"
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "Consolidation completed successfully at $(date -Iseconds)"
else
    echo "Consolidation failed with exit code $EXIT_CODE at $(date -Iseconds)" >&2
fi

exit $EXIT_CODE
