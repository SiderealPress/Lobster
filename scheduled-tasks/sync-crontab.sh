#!/bin/bash
# Lobster Crontab Synchronizer
# Syncs jobs.json to system crontab

set -e

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
REPO_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
JOBS_FILE="$WORKSPACE/scheduled-jobs/jobs.json"
RUNNER="$REPO_DIR/scheduled-tasks/dispatch-job.sh"

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

# Generate new crontab entries from jobs.json.
# Each job uses its own .runner if set, otherwise falls back to the default RUNNER.
# This allows specific jobs (e.g. bot-talk-poller) to use a lightweight pre-check
# wrapper that avoids dispatching to the LLM when there is nothing to process.
if command -v jq &> /dev/null; then
    CRON_ENTRIES=$(jq -r --arg runner "$RUNNER" --arg marker "$MARKER" \
        --arg repo_dir "$REPO_DIR" '
        .jobs | to_entries[] |
        select(.value.enabled == true) |
        (.value.runner // $runner) as $job_runner |
        # Expand $REPO_DIR placeholder so per-job runners can reference the install dir
        ($job_runner | gsub("\\$REPO_DIR"; $repo_dir)) as $resolved_runner |
        "\(.value.schedule) \($resolved_runner) \(.key) \($marker)"
    ' "$JOBS_FILE" 2>/dev/null || echo "")
else
    CRON_ENTRIES=$(python3 -c "
import json
import sys
import os
try:
    with open('$JOBS_FILE', 'r') as f:
        data = json.load(f)
    repo_dir = '$REPO_DIR'
    default_runner = '$RUNNER'
    for name, job in data.get('jobs', {}).items():
        if job.get('enabled', True):
            schedule = job.get('schedule', '')
            if schedule:
                runner = job.get('runner', default_runner)
                runner = runner.replace('\$REPO_DIR', repo_dir)
                print(f\"{schedule} {runner} {name} $MARKER\")
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
