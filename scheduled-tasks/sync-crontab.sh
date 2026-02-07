#!/bin/bash
# Lobster Crontab Synchronizer
# Syncs jobs.json to system crontab

set -e

JOBS_FILE="$HOME/lobster/scheduled-tasks/jobs.json"
RUNNER="$HOME/lobster/scheduled-tasks/run-job.sh"

# Check if crontab is available
if ! command -v crontab &> /dev/null; then
    echo "Warning: crontab command not found. Install cron to enable scheduled tasks."
    echo "On Debian/Ubuntu: sudo apt-get install cron"
    echo "Jobs are saved and will be synced when cron is available."
    exit 0
fi

if [ ! -f "$JOBS_FILE" ]; then
    echo "Error: Jobs file not found: $JOBS_FILE"
    exit 1
fi

# Marker for lobster-managed cron entries
MARKER="# LOBSTER-SCHEDULED"

# Get existing crontab entries (excluding lobster ones)
EXISTING=$(crontab -l 2>/dev/null | grep -v "$MARKER" | grep -v "$RUNNER" || true)

# Generate new crontab entries from jobs.json
if command -v jq &> /dev/null; then
    CRON_ENTRIES=$(jq -r --arg runner "$RUNNER" --arg marker "$MARKER" '
        .jobs | to_entries[] |
        select(.value.enabled == true) |
        "\(.value.schedule) \($runner) \(.key) \($marker)"
    ' "$JOBS_FILE" 2>/dev/null || echo "")
else
    CRON_ENTRIES=$(python3 -c "
import json
import sys
try:
    with open('$JOBS_FILE', 'r') as f:
        data = json.load(f)
    for name, job in data.get('jobs', {}).items():
        if job.get('enabled', True):
            schedule = job.get('schedule', '')
            if schedule:
                print(f\"{schedule} $RUNNER {name} $MARKER\")
except Exception as e:
    sys.stderr.write(f'Error: {e}\n')
" 2>/dev/null || echo "")
fi

# Build new crontab
{
    if [ -n "$EXISTING" ]; then
        echo "$EXISTING"
    fi
    if [ -n "$CRON_ENTRIES" ]; then
        echo "$CRON_ENTRIES"
    fi
} | crontab -

# Show result
echo "Crontab synchronized:"
crontab -l 2>/dev/null | grep "$MARKER" || echo "(no lobster jobs)"
