#!/bin/bash
#===============================================================================
# Cron Job Post-Reminder
#
# Called by run-job.sh after each scheduled job completes. Writes a lightweight
# cron_reminder message to the Lobster inbox so the dispatcher is notified that
# a job finished and output is available to review.
#
# Usage: post-reminder.sh <job-name> <exit-code> <duration-seconds>
#
# The dispatcher handles cron_reminder by calling check_task_outputs for the
# named job and surfacing the result to the user if noteworthy.
#===============================================================================

set -e

JOB_NAME="$1"
EXIT_CODE="${2:-0}"
DURATION_SECONDS="${3:-0}"

if [ -z "$JOB_NAME" ]; then
    echo "Usage: $0 <job-name> <exit-code> <duration-seconds>"
    exit 1
fi

INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
mkdir -p "$INBOX_DIR"

# Derive status string from exit code
if [ "$EXIT_CODE" -eq 0 ]; then
    STATUS="success"
else
    STATUS="failed"
fi

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%S.%6N)
EPOCH_MS=$(date +%s%3N)
MSG_ID="${EPOCH_MS}_cron_${JOB_NAME}"

uv run - \
    "${INBOX_DIR}/${MSG_ID}.json" \
    "${MSG_ID}" \
    "${TIMESTAMP}" \
    "${JOB_NAME}" \
    "${EXIT_CODE}" \
    "${DURATION_SECONDS}" \
    "${STATUS}" \
    << 'PYEOF'
import json, sys
out_path = sys.argv[1]
msg_id = sys.argv[2]
timestamp = sys.argv[3]
job_name = sys.argv[4]
exit_code = int(sys.argv[5])
duration_seconds = int(sys.argv[6])
status = sys.argv[7]

msg = {
    "id": msg_id,
    "source": "system",
    "type": "scheduled_reminder",
    "chat_id": 0,
    "user_id": 0,
    "username": "lobster-cron",
    "user_name": "Cron",
    "text": f"[Cron] Job '{job_name}' finished ({status}, {duration_seconds}s)",
    "job_name": job_name,
    "exit_code": exit_code,
    "duration_seconds": duration_seconds,
    "status": status,
    "timestamp": timestamp,
}

tmp_path = out_path + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(msg, f, ensure_ascii=False, indent=2)
    f.flush()

import os
os.replace(tmp_path, out_path)
PYEOF

echo "Reminder posted for job: $JOB_NAME (status=$STATUS, duration=${DURATION_SECONDS}s)"
