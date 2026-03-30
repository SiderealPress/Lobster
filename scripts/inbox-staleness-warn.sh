#!/bin/bash
# inbox-staleness-warn.sh — Inject an inbox-staleness warning when the oldest
# unprocessed user message has been waiting for 3+ minutes.
#
# Design: pure shell, no Python, no Claude. Safe to call from cron every minute.
#
# Filtering logic mirrors health-check-v3.sh lines ~107-114:
#   - Only user-facing sources (telegram, sms, signal, slack) are counted.
#   - Only genuine user-originated message types (message, photo, image, voice,
#     audio, callback, text, document) are counted.
#   - Internal queue messages (subagent_result, subagent_error, scheduled_reminder,
#     system, compact, etc.) are excluded even when they carry source="telegram".
#
# Dedup logic mirrors post-reminder.sh:
#   - Check both inbox/ and processing/ for an existing message with the same
#     reminder_type. If found, exit silently without injecting a duplicate.

set -euo pipefail

INBOX_DIR="${HOME}/messages/inbox"
PROCESSING_DIR="${HOME}/messages/processing"

REMINDER_TYPE="inbox_staleness_warn"
STALE_THRESHOLD_SECONDS=180   # 3 minutes

# User-facing sources — matches health-check-v3.sh line ~108
USER_FACING_SOURCES="telegram sms signal slack"

# Genuine user-originated message types — matches health-check-v3.sh line ~114
USER_FACING_TYPES="message photo image voice audio callback text document"

mkdir -p "$INBOX_DIR"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

is_user_facing_source() {
    local source="$1"
    local s
    for s in $USER_FACING_SOURCES; do
        [[ "$source" == "$s" ]] && return 0
    done
    return 1
}

is_user_facing_type() {
    local type="$1"
    local t
    for t in $USER_FACING_TYPES; do
        [[ "$type" == "$t" ]] && return 0
    done
    return 1
}

# ---------------------------------------------------------------------------
# Dedup: skip if a staleness warning is already pending (inbox or processing).
# ---------------------------------------------------------------------------
if grep -rl "\"reminder_type\": \"${REMINDER_TYPE}\"" "$INBOX_DIR" "$PROCESSING_DIR" 2>/dev/null | grep -q .; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Scan inbox for user-facing messages and find the oldest.
# ---------------------------------------------------------------------------
now=$(date +%s)
oldest_age=0

while IFS= read -r -d '' f; do
    # Parse source and type via jq; skip if missing or unparseable.
    source=$(jq -r '.source // empty' "$f" 2>/dev/null)
    type=$(jq -r '.type // empty' "$f" 2>/dev/null)

    [[ -z "$source" ]] && continue
    is_user_facing_source "$source" || continue
    if [[ -n "$type" ]] && ! is_user_facing_type "$type"; then
        continue
    fi

    file_time=$(stat -c %Y "$f" 2>/dev/null) || continue
    [[ -z "$file_time" ]] && continue

    age=$(( now - file_time ))
    [[ $age -gt $oldest_age ]] && oldest_age=$age
done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

# No user-facing messages, or none old enough — nothing to warn about.
[[ $oldest_age -lt $STALE_THRESHOLD_SECONDS ]] && exit 0

# ---------------------------------------------------------------------------
# Inject the warning message.
# ---------------------------------------------------------------------------
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
MILLIS="$(date +%s%3N)"
FILENAME="${INBOX_DIR}/${MILLIS}_reminder_${REMINDER_TYPE}.json"

cat > "$FILENAME" <<EOF
{
  "type": "scheduled_reminder",
  "reminder_type": "${REMINDER_TYPE}",
  "source": "system",
  "chat_id": 0,
  "text": "Inbox stale for 3 minutes — call wait_for_messages now, or delegate to a subagent. Do not continue inline work.",
  "timestamp": "${TIMESTAMP}"
}
EOF
