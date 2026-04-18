#!/bin/bash
# granola-sync.sh — Scheduled Granola → Obsidian sync job
#
# Runs the Python sync script and writes output to the Lobster task output system.
# Called by the systemd timer every 30 minutes.
#
# Logs: ~/lobster-workspace/scheduled-jobs/logs/granola-sync-*.log

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
LOG_FILE="$LOG_DIR/granola-sync-${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"

echo "[$(date -Iseconds)] granola-sync starting" | tee "$LOG_FILE"

# Run the sync script
cd "$LOBSTER_DIR"
set +e
OUTPUT=$(uv run python src/integrations/granola/sync.py 2>&1)
EXIT_CODE=$?
set -e

echo "$OUTPUT" | tee -a "$LOG_FILE"
echo "[$(date -Iseconds)] granola-sync exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

exit $EXIT_CODE
