#!/bin/bash
#===============================================================================
# Docker Worker Entrypoint
#
# Runs claude -p with the given prompt, captures output, and writes result
# as a JSON message to the inbox (bind-mounted from host).
#
# Required env vars:
#   WORKER_JOB_NAME    - Identifier for this job
#   WORKER_CHAT_ID     - Chat ID to reply to
#   WORKER_SOURCE      - Source platform (telegram, slack, etc.)
#   WORKER_MAX_TURNS   - Max agentic turns for claude
#   WORKER_PROMPT      - The prompt to send to claude
#   ANTHROPIC_API_KEY  - API key for Claude
#===============================================================================

set -o pipefail

INBOX_DIR="/home/worker/messages/inbox"

#===============================================================================
# Validation
#===============================================================================
missing=""
[[ -z "$WORKER_JOB_NAME" ]]  && missing="$missing WORKER_JOB_NAME"
[[ -z "$WORKER_CHAT_ID" ]]   && missing="$missing WORKER_CHAT_ID"
[[ -z "$WORKER_SOURCE" ]]    && missing="$missing WORKER_SOURCE"
[[ -z "$WORKER_MAX_TURNS" ]] && missing="$missing WORKER_MAX_TURNS"
[[ -z "$WORKER_PROMPT" ]]    && missing="$missing WORKER_PROMPT"
[[ -z "$ANTHROPIC_API_KEY" ]] && missing="$missing ANTHROPIC_API_KEY"

if [[ -n "$missing" ]]; then
    echo "ERROR: Missing required env vars:$missing" >&2
    exit 1
fi

#===============================================================================
# Run Claude
#===============================================================================
# Prevent "cannot launch inside another Claude Code session" error.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null || true

start_time=$(date +%s)
echo "Worker starting: job=$WORKER_JOB_NAME, max_turns=$WORKER_MAX_TURNS"

output=$(timeout 600 claude -p "$WORKER_PROMPT" \
    --dangerously-skip-permissions \
    --max-turns "$WORKER_MAX_TURNS" \
    --output-format text 2>&1)
exit_code=$?

end_time=$(date +%s)
duration=$((end_time - start_time))

if [[ $exit_code -eq 0 ]]; then
    status="success"
    echo "Worker completed successfully in ${duration}s"
else
    status="failed"
    echo "Worker failed with exit code $exit_code after ${duration}s"
fi

#===============================================================================
# Write result as inbox message (atomic: write tmp then mv)
#===============================================================================
epoch_ms=$(date +%s%3N)
msg_id="${epoch_ms}_worker_${WORKER_JOB_NAME}"
msg_file="$INBOX_DIR/${msg_id}.json"
tmp_file="${msg_file}.tmp"

# Truncate output if too large (100KB limit for message text)
max_output_bytes=102400
if [[ ${#output} -gt $max_output_bytes ]]; then
    output="${output:0:$max_output_bytes}

[OUTPUT TRUNCATED - exceeded ${max_output_bytes} bytes]"
fi

# Escape output for JSON (handle newlines, quotes, backslashes, tabs, control chars)
json_output=$(printf '%s' "$output" | jq -Rs .)

cat > "$tmp_file" <<ENDJSON
{
  "id": "${msg_id}",
  "source": "worker",
  "chat_id": ${WORKER_CHAT_ID},
  "user_id": 0,
  "username": "docker-worker",
  "user_name": "Worker: ${WORKER_JOB_NAME}",
  "text": ${json_output},
  "timestamp": "$(date -Iseconds)",
  "type": "text",
  "worker_metadata": {
    "type": "worker_result",
    "job_name": "${WORKER_JOB_NAME}",
    "reply_chat_id": ${WORKER_CHAT_ID},
    "reply_source": "${WORKER_SOURCE}",
    "duration_seconds": ${duration},
    "status": "${status}",
    "exit_code": ${exit_code}
  }
}
ENDJSON

mv "$tmp_file" "$msg_file"
echo "Result written to inbox: $msg_id"
