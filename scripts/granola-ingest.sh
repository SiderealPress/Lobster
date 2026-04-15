#!/bin/bash
#===============================================================================
# granola-ingest.sh — Wrapper for the Granola incremental ingest job
#
# Runs every 15 minutes via cron. Sources config.env for GRANOLA_API_KEY.
# Logs to ~/lobster-workspace/granola-notes/ingest.log (via the Python script).
#
# Register with:
#   ~/lobster/scripts/cron-manage.sh add \
#     "# LOBSTER-GRANOLA-INGEST" \
#     "*/15 * * * * ~/lobster/scripts/granola-ingest.sh # LOBSTER-GRANOLA-INGEST"
#
# Remove with:
#   ~/lobster/scripts/cron-manage.sh remove "# LOBSTER-GRANOLA-INGEST"
#===============================================================================

set -euo pipefail

LOBSTER_CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
CONFIG_ENV="$LOBSTER_CONFIG_DIR/config.env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKS_DIR="$(cd "$SCRIPT_DIR/../scheduled-tasks" && pwd)"

# Source config.env to load GRANOLA_API_KEY and other env vars
if [ -f "$CONFIG_ENV" ]; then
    # Export only non-empty variable assignments, skip comments and blanks
    set -a
    # shellcheck disable=SC1090
    source "$CONFIG_ENV"
    set +a
fi

# Validate required var
if [ -z "${GRANOLA_API_KEY:-}" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [ERROR] GRANOLA_API_KEY not set in $CONFIG_ENV" >&2
    exit 2
fi

# Run the ingest script using uv
exec uv run --project "$SCRIPT_DIR/.." "$TASKS_DIR/granola_ingest.py"
