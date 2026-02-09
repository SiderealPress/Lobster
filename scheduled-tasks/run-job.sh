#!/bin/bash
# Lobster Scheduled Task Executor
# Runs a scheduled job in a fresh Claude instance

set -e

# Ensure Claude is in PATH (cron doesn't inherit user PATH)
export PATH="$HOME/.local/bin:$PATH"

JOB_NAME="$1"

if [ -z "$JOB_NAME" ]; then
    echo "Usage: $0 <job-name>"
    exit 1
fi

JOBS_DIR="$HOME/lobster/scheduled-tasks"
TASK_FILE="$JOBS_DIR/tasks/${JOB_NAME}.md"
OUTPUT_DIR="$HOME/messages/task-outputs"
LOG_DIR="$JOBS_DIR/logs"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
JOBS_FILE="$JOBS_DIR/jobs.json"

# Ensure directories exist
mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

# Check task file exists
if [ ! -f "$TASK_FILE" ]; then
    echo "Error: Task file not found: $TASK_FILE"
    exit 1
fi

# Read task content
TASK_CONTENT=$(cat "$TASK_FILE")

# Log file for this execution
LOG_FILE="$LOG_DIR/${JOB_NAME}-${TIMESTAMP}.log"

# Record start time
START_TIME=$(date +%s)
START_ISO=$(date -Iseconds)

echo "[$START_ISO] Starting job: $JOB_NAME" | tee "$LOG_FILE"

# Run Claude with the task
# The task instructions tell Claude to call write_task_output with results
claude -p "$TASK_CONTENT

---

IMPORTANT: You are running as a scheduled task. When you complete your task:
1. Call write_task_output() with your results summary
2. Keep output concise - the main Lobster instance will review this later
3. Exit after writing output - do not start a loop" \
    --dangerously-skip-permissions \
    --max-turns 15 \
    2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=$?

# Record end time
END_TIME=$(date +%s)
END_ISO=$(date -Iseconds)
DURATION=$((END_TIME - START_TIME))

echo "" | tee -a "$LOG_FILE"
echo "[$END_ISO] Job completed in ${DURATION}s with exit code: $EXIT_CODE" | tee -a "$LOG_FILE"

# Update jobs.json with last_run info
if [ -f "$JOBS_FILE" ]; then
    # Use jq if available, otherwise use Python
    if command -v jq &> /dev/null; then
        STATUS="success"
        [ $EXIT_CODE -ne 0 ] && STATUS="failed"

        TMP_FILE=$(mktemp)
        jq --arg name "$JOB_NAME" \
           --arg last_run "$END_ISO" \
           --arg status "$STATUS" \
           '.jobs[$name].last_run = $last_run | .jobs[$name].last_status = $status' \
           "$JOBS_FILE" > "$TMP_FILE" && mv "$TMP_FILE" "$JOBS_FILE"
    else
        python3 -c "
import json
import sys
with open('$JOBS_FILE', 'r') as f:
    data = json.load(f)
if '$JOB_NAME' in data.get('jobs', {}):
    data['jobs']['$JOB_NAME']['last_run'] = '$END_ISO'
    data['jobs']['$JOB_NAME']['last_status'] = 'success' if $EXIT_CODE == 0 else 'failed'
    with open('$JOBS_FILE', 'w') as f:
        json.dump(data, f, indent=2)
"
    fi
fi

exit $EXIT_CODE
