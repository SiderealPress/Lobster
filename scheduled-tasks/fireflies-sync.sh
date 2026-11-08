#!/bin/bash
# fireflies-sync.sh — Scheduled Fireflies → Obsidian sync job
#
# Runs the Python sync script and writes output to the Lobster task output system.
# Intended to be called by a systemd timer every 30 minutes (mirrors granola-sync.sh).
#
# Logs: ~/lobster-workspace/scheduled-jobs/logs/fireflies-sync-*.log

set -euo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# --- Load env ---
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
for _env_file in "$CONFIG_DIR/config.env" "$CONFIG_DIR/global.env"; do
    if [ -f "$_env_file" ]; then
        set -a
        # shellcheck source=/dev/null
        source "$_env_file"
        set +a
    fi
done
unset _env_file

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOBSTER_DIR="${LOBSTER_DIR:-$HOME/lobster}"
LOG_DIR="$WORKSPACE/scheduled-jobs/logs"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$LOG_DIR/fireflies-sync-${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"

if [ -z "${FIREFLIES_API_KEY:-}" ]; then
    echo "[$(date -Iseconds)] fireflies-sync: FIREFLIES_API_KEY not set — skipping run" | tee "$LOG_FILE"
    exit 0
fi

echo "[$(date -Iseconds)] fireflies-sync starting" | tee "$LOG_FILE"

# Run the sync script
#
# Invoked by file path (not `-m integrations.fireflies.sync`) to mirror
# granola-sync.sh. Module-mode invocation requires `integrations` to
# already be importable from sys.path before the interpreter starts, which
# depends on the editable install being wired up correctly. Running by
# file path lets Python add the script's own directory to sys.path, and
# sync.py's own `sys.path.insert(0, str(_SRC_DIR))` (pointing at src/) then
# makes the `integrations.*` absolute imports resolve — the same pattern
# granola/sync.py already relies on.
cd "$LOBSTER_DIR"
set +e
OUTPUT=$(uv run python src/integrations/fireflies/sync.py 2>&1)
EXIT_CODE=$?
set -e

echo "$OUTPUT" | tee -a "$LOG_FILE"
echo "[$(date -Iseconds)] fireflies-sync exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

exit $EXIT_CODE
