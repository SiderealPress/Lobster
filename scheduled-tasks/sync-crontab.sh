#!/bin/bash
# Lobster Crontab Synchronizer
# Syncs jobs.json to the user's crontab.
#
# This script runs as a subprocess of the MCP server, which sets
# PR_SET_NO_NEW_PRIVS (NoNewPrivs=1).  That flag suppresses setgid bits,
# so `crontab -` fails with "mkstemp: Permission denied" even though the
# crontab binary is setgid-crontab.
#
# Primary path: write directly to /var/spool/cron/crontabs/$USER (requires
# the lobster user to be in the `crontab` group — see upgrade.sh Migration 41).
# Fallback path: use `crontab -` for systems where the user is not yet in the
# crontab group (e.g. before the migration runs or on non-Debian systems).

set -e

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
REPO_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
JOBS_FILE="$WORKSPACE/scheduled-jobs/jobs.json"
RUNNER="$REPO_DIR/scheduled-tasks/dispatch-job.sh"
CRONTAB_FILE="/var/spool/cron/crontabs/$(whoami)"

# Check if cron is available at all
if ! command -v crontab &> /dev/null && [ ! -d "/var/spool/cron/crontabs" ]; then
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

# Read existing crontab entries, stripping any lobster-managed lines.
# Try direct file read first (available when user is in the crontab group),
# then fall back to `crontab -l` (requires setgid to work).
read_existing_crontab() {
    if [ -f "$CRONTAB_FILE" ] && [ -r "$CRONTAB_FILE" ]; then
        cat "$CRONTAB_FILE"
    elif command -v crontab &> /dev/null; then
        crontab -l 2>/dev/null || true
    fi
}

EXISTING=$(read_existing_crontab | grep -v "$MARKER" | grep -v "$RUNNER" || true)

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

# Build the new crontab content
NEW_CRONTAB=""
if [ -n "$EXISTING" ]; then
    NEW_CRONTAB="$EXISTING"
fi
if [ -n "$CRON_ENTRIES" ]; then
    if [ -n "$NEW_CRONTAB" ]; then
        NEW_CRONTAB="$NEW_CRONTAB
$CRON_ENTRIES"
    else
        NEW_CRONTAB="$CRON_ENTRIES"
    fi
fi

# Write the crontab.
# Primary: write directly to the crontab file (works under NoNewPrivs=1 when
# the user is in the crontab group, because the directory is group-writable).
# Fallback: pipe through `crontab -` (works in regular shell sessions where
# the crontab setgid bit is effective).
write_crontab() {
    local content="$1"
    # Direct write: requires crontab group membership (upgrade.sh Migration 41).
    if [ -d "/var/spool/cron/crontabs" ] && [ -w "/var/spool/cron/crontabs" ]; then
        # Write atomically: temp file in same directory, then rename.
        local tmpfile
        tmpfile=$(mktemp "/var/spool/cron/crontabs/.tmp.XXXXXX")
        chmod 0600 "$tmpfile"
        printf '%s\n' "$content" > "$tmpfile"
        mv "$tmpfile" "$CRONTAB_FILE"
        return 0
    fi
    # Fallback: crontab binary (requires setgid, blocked under NoNewPrivs=1).
    if command -v crontab &> /dev/null; then
        printf '%s\n' "$content" | crontab -
        return $?
    fi
    echo "Error: cannot write crontab — not in crontab group and crontab binary unavailable" >&2
    return 1
}

write_crontab "$NEW_CRONTAB"

# Show result
echo "Crontab synchronized:"
read_existing_crontab | grep "$MARKER" || echo "(no lobster jobs)"
