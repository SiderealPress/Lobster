#!/usr/bin/env bash
# get-restart-reason.sh — print the reason the last Lobster session started.
#
# Reads ~/lobster-workspace/data/last-restart-reason.json and prints a
# human-readable summary.  The file is written by two sources:
#
#   - on-compact.py (reason="compaction") — fired by Claude Code on context
#     compaction.  Indicates the session started because CC compacted the
#     dispatcher's context window.
#
#   - health-check-v3.sh (reason="health-check") — fired before a systemd
#     restart.  Indicates the session started because the health check
#     detected an unhealthy state (stale inbox, dead process, etc.).
#
# Exit codes:
#   0  — file found and parsed successfully
#   1  — file absent or unreadable (system has not restarted or compacted yet)
#
# Usage:
#   ~/lobster/scripts/get-restart-reason.sh
#   Reason: compaction
#   Timestamp: 2026-05-01T01:05:53Z
#
#   # Or pipe to jq for structured output:
#   ~/lobster/scripts/get-restart-reason.sh --json | jq .

set -euo pipefail

REASON_FILE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}/data/last-restart-reason.json"

if [[ "${1:-}" == "--json" ]]; then
    if [[ -f "$REASON_FILE" ]]; then
        cat "$REASON_FILE"
        exit 0
    else
        printf '{"reason": null, "ts": null, "note": "file absent"}\n'
        exit 1
    fi
fi

if [[ ! -f "$REASON_FILE" ]]; then
    echo "last-restart-reason.json not found (system has not restarted or compacted yet)"
    exit 1
fi

reason=$(python3 -c "import json,sys; d=json.load(open('$REASON_FILE')); print(d.get('reason','unknown'))" 2>/dev/null || echo "unknown")
ts=$(python3 -c "import json,sys; d=json.load(open('$REASON_FILE')); print(d.get('ts','unknown'))" 2>/dev/null || echo "unknown")

echo "Reason:    $reason"
echo "Timestamp: $ts"
