#!/bin/bash
# Lobster Scheduled Job Dispatcher
#
# Writes a scheduled_reminder message into the Lobster inbox so the dispatcher
# spawns a subagent for the job. Does NOT invoke Claude directly.
#
# Usage: dispatch-job.sh <job-name>

set -e

# Ensure uv and other tools are in PATH (cron doesn't inherit user PATH)
export PATH="$HOME/.local/bin:$PATH"

JOB_NAME="$1"

if [ -z "$JOB_NAME" ]; then
    echo "Usage: $0 <job-name>"
    exit 1
fi

# Load env files so tokens and config are available when running from cron.
# Source config.env first, then global.env (global overrides config).
CONFIG_DIR="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}"
for _env_file in "$CONFIG_DIR/config.env" "$CONFIG_DIR/global.env"; do
    if [ -f "$_env_file" ]; then
        # shellcheck source=/dev/null
        set -a
        source "$_env_file"
        set +a
    fi
done
unset _env_file

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
TASK_FILE="$WORKSPACE/scheduled-jobs/tasks/${JOB_NAME}.md"
LOG_DIR="$WORKSPACE/scheduled-jobs/logs"
INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
START_ISO=$(date -Iseconds)

# Ensure log directory exists
mkdir -p "$LOG_DIR" "$INBOX_DIR"

LOG_FILE="$LOG_DIR/${JOB_NAME}-${TIMESTAMP}.log"

# --- Check enabled flag ---
# Query systemd for the enabled state of this job's timer unit.
# If the unit doesn't exist or is disabled, exit silently.
if ! systemctl is-enabled --quiet "lobster-${JOB_NAME}.timer" 2>/dev/null; then
    echo "[$START_ISO] Job '$JOB_NAME' is disabled — skipping" >> "$LOG_FILE" 2>&1 || true
    exit 0
fi

echo "[$START_ISO] Posting dispatch for job: $JOB_NAME" | tee "$LOG_FILE"

# --- Check task file exists ---
# If missing, auto-disable the job in jobs.json so cron stops dispatching it,
# then exit 0 so cron doesn't keep logging errors (#1200).
if [ ! -f "$TASK_FILE" ]; then
    echo "[$START_ISO] Error: Task file not found: $TASK_FILE — auto-disabling job '$JOB_NAME'" | tee -a "$LOG_FILE"
    if [ -f "$JOBS_FILE" ]; then
        JOBS_LOCK="${JOBS_FILE}.lock"
        (
            flock -x 9
            if command -v jq &> /dev/null; then
                TMP_FILE=$(mktemp)
                jq --arg name "$JOB_NAME" \
                   '.jobs[$name].enabled = false' \
                   "$JOBS_FILE" > "$TMP_FILE" && mv "$TMP_FILE" "$JOBS_FILE"
            else
                uv run - \
                    "$JOBS_FILE" \
                    "$JOB_NAME" \
                    << 'PYEOF'
import json, os, sys
jobs_file = sys.argv[1]
job_name  = sys.argv[2]
with open(jobs_file) as f:
    data = json.load(f)
if job_name in data.get('jobs', {}):
    data['jobs'][job_name]['enabled'] = False
    tmp = jobs_file + '.tmp.' + str(os.getpid())
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, jobs_file)
PYEOF
            fi
        ) 9>"$JOBS_LOCK" 2>/dev/null || true
        echo "[$START_ISO] Job '$JOB_NAME' auto-disabled in jobs.json (task file missing)" | tee -a "$LOG_FILE"
    fi
    exit 0
fi

# --- Dedup guard: skip if a pending dispatch already exists in inbox (#1201) ---
# This prevents inbox flooding when a job's subagent takes longer than the schedule interval.
if ls "${INBOX_DIR}"/*_scheduled_"${JOB_NAME}".json 2>/dev/null | head -1 | grep -q .; then
    echo "[$START_ISO] Job '$JOB_NAME' already has a pending dispatch in inbox — skipping" | tee -a "$LOG_FILE"
    exit 0
fi

# --- Write scheduled_reminder to inbox ---
# Embed the task content so the dispatcher can pass it directly to the
# subagent without needing to read the file on the main thread.
EPOCH_MS=$(date +%s%3N)
MSG_ID="${EPOCH_MS}_scheduled_${JOB_NAME}"

uv run - \
    "${INBOX_DIR}/${MSG_ID}.json" \
    "${MSG_ID}" \
    "${START_ISO}" \
    "${JOB_NAME}" \
    "${TASK_FILE}" \
    << 'PYEOF'
import json, sys, os

out_path   = sys.argv[1]
msg_id     = sys.argv[2]
timestamp  = sys.argv[3]
job_name   = sys.argv[4]
task_file  = sys.argv[5]

with open(task_file) as f:
    task_content = f.read()

msg = {
    "id": msg_id,
    "source": "system",
    "type": "scheduled_reminder",
    "chat_id": 0,
    "user_id": 0,
    "username": "lobster-cron",
    "user_name": "Cron",
    "text": f"[Cron] Dispatch job '{job_name}'",
    "reminder_type": job_name,
    "job_name": job_name,
    "task_content": task_content,
    "timestamp": timestamp,
}

tmp_path = out_path + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(msg, f, ensure_ascii=False, indent=2)
    f.flush()

os.replace(tmp_path, out_path)
print(f"Dispatch posted: {out_path}")
PYEOF

echo "[$START_ISO] Dispatch posted for job: $JOB_NAME — dispatcher will spawn subagent" | tee -a "$LOG_FILE"

exit 0
