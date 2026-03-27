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
JOBS_FILE="$WORKSPACE/scheduled-jobs/jobs.json"
TASK_FILE="$WORKSPACE/scheduled-jobs/tasks/${JOB_NAME}.md"
LOG_DIR="$WORKSPACE/scheduled-jobs/logs"
INBOX_DIR="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
START_ISO=$(date -Iseconds)

# Ensure log directory exists
mkdir -p "$LOG_DIR" "$INBOX_DIR"

LOG_FILE="$LOG_DIR/${JOB_NAME}-${TIMESTAMP}.log"

# --- Check enabled flag ---
# If the job is marked enabled=false in jobs.json, exit silently.
if [ -f "$JOBS_FILE" ]; then
    ENABLED=$(python3 -c "
import json, sys
try:
    with open('$JOBS_FILE') as f:
        data = json.load(f)
    job = data.get('jobs', {}).get('$JOB_NAME', {})
    print(str(job.get('enabled', True)).lower())
except Exception:
    print('true')
" 2>/dev/null)
    if [ "$ENABLED" = "false" ]; then
        echo "[$START_ISO] Job '$JOB_NAME' is disabled — skipping" >> "$LOG_FILE" 2>&1 || true
        exit 0
    fi
fi

echo "[$START_ISO] Posting dispatch for job: $JOB_NAME" | tee "$LOG_FILE"

# --- Check task file exists ---
if [ ! -f "$TASK_FILE" ]; then
    echo "[$START_ISO] Error: Task file not found: $TASK_FILE" | tee -a "$LOG_FILE"
    exit 1
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

jobs_file = os.environ.get("LOBSTER_WORKSPACE", os.path.expanduser("~/lobster-workspace")) + "/scheduled-jobs/jobs.json"
job_chat_id = 0
try:
    import json as _json
    with open(jobs_file) as _jf:
        _jobs_data = _json.load(_jf)
    _job_record = _jobs_data.get("jobs", {}).get(job_name, {})
    job_chat_id = _job_record.get("chat_id", 0)
except Exception:
    pass

msg = {
    "id": msg_id,
    "source": "system",
    "type": "scheduled_reminder",
    "chat_id": job_chat_id,
    "user_id": job_chat_id,
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

# Update jobs.json last_run to reflect when the job was dispatched
if [ -f "$JOBS_FILE" ]; then
    if command -v jq &> /dev/null; then
        TMP_FILE=$(mktemp)
        jq --arg name "$JOB_NAME" \
           --arg last_run "$START_ISO" \
           'if .jobs[$name] then .jobs[$name].last_run = $last_run else . end' \
           "$JOBS_FILE" > "$TMP_FILE" && mv "$TMP_FILE" "$JOBS_FILE"
    else
        python3 -c "
import json
with open('$JOBS_FILE') as f:
    data = json.load(f)
if '$JOB_NAME' in data.get('jobs', {}):
    data['jobs']['$JOB_NAME']['last_run'] = '$START_ISO'
    with open('$JOBS_FILE', 'w') as f:
        json.dump(data, f, indent=2)
" 2>/dev/null || true
    fi
fi

exit 0
