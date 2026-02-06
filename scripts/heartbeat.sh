#!/bin/bash
#===============================================================================
# Hyperion Heartbeat - Touch the heartbeat file to signal Claude is alive
#
# This script should be called periodically by the Claude session to indicate
# it is actively processing messages. The health check monitors this file.
#
# Usage: ~/hyperion/scripts/heartbeat.sh [optional_status_message]
#===============================================================================

HEARTBEAT_FILE="$HOME/hyperion-workspace/logs/claude-heartbeat"
HEARTBEAT_LOG="$HOME/hyperion-workspace/logs/heartbeat.log"

# Ensure directory exists
mkdir -p "$(dirname "$HEARTBEAT_FILE")"

# Touch the heartbeat file
touch "$HEARTBEAT_FILE"

# Optionally log status
if [[ -n "$1" ]]; then
    echo "[$(date -Iseconds)] $1" >> "$HEARTBEAT_LOG"
fi

# Keep heartbeat log from growing too large (last 100 lines)
if [[ -f "$HEARTBEAT_LOG" ]]; then
    tail -100 "$HEARTBEAT_LOG" > "$HEARTBEAT_LOG.tmp" && mv "$HEARTBEAT_LOG.tmp" "$HEARTBEAT_LOG"
fi

echo "OK"
