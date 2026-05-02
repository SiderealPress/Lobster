#!/bin/bash
#===============================================================================
# Health check patch: 5-state dispatcher state machine check
#
# Prototype for issue #1918.
#
# This function reads dispatcher-state.json and applies per-state health logic:
#   STARTING     - healthy if age < 120s (startup is slow)
#   WAITING      - use existing heartbeat check (WFM daemon covers this)
#   PROCESSING   - healthy if updated_at < 1200s ago (long inference is OK)
#   WINDING_DOWN - healthy, do not restart (session ending gracefully)
#   DEAD         - RED, restart immediately (dispatcher exited)
#   missing/unknown - fall back to current behavior
#
# Returns:
#   0 = GREEN (healthy or check skipped)
#   1 = YELLOW (warning only)
#   2 = RED (restart required)
#   3 = SKIP (fall through to existing check)
#===============================================================================

DISPATCHER_STATE_FILE="${LOBSTER_DISPATCHER_STATE_FILE_OVERRIDE:-$WORKSPACE_DIR/data/dispatcher-state.json}"
DISPATCHER_STATE_PROCESSING_STALE_SECONDS=1200  # 20 min — covers long inference
DISPATCHER_STATE_STARTING_STALE_SECONDS=120      # 2 min — startup grace

check_dispatcher_state() {
    if [[ ! -f "$DISPATCHER_STATE_FILE" ]]; then
        log_info "Dispatcher state: no state file found — skipping state-machine check (fresh install?)"
        return 3  # SKIP: fall through to existing heartbeat check
    fi

    # Read all needed fields in a single Python process invocation.
    local parsed
    parsed=$(uv run python3 -c "
import json, sys
from datetime import datetime, timezone, timedelta
import time
try:
    d = json.load(open('$DISPATCHER_STATE_FILE'))
    state = d.get('state', 'unknown')
    # Use 'since' (state-entry time) if present, else fall back to 'updated_at'.
    ts = d.get('since') or d.get('updated_at', '')
    age = 0
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            age = int(time.time() - dt.timestamp())
        except Exception:
            age = 0
    print(state)
    print(age)
except Exception as e:
    print('unknown')
    print(0)
" 2>/dev/null)

    local state age
    state=$(echo "$parsed" | head -1)
    age=$(echo "$parsed" | tail -1)

    if [[ -z "$state" || "$state" == "unknown" ]]; then
        log_info "Dispatcher state: unreadable — skipping state-machine check"
        return 3  # SKIP
    fi

    case "$state" in
        STARTING)
            if [[ $age -gt $DISPATCHER_STATE_STARTING_STALE_SECONDS ]]; then
                log_error "RED: dispatcher state=STARTING for ${age}s (threshold: ${DISPATCHER_STATE_STARTING_STALE_SECONDS}s) — stuck in startup"
                return 2
            fi
            log_info "Dispatcher state=STARTING OK: ${age}s old (threshold: ${DISPATCHER_STATE_STARTING_STALE_SECONDS}s)"
            return 0
            ;;
        WAITING)
            log_info "Dispatcher state=WAITING: defer to heartbeat check"
            return 3  # SKIP: fall through to existing heartbeat check
            ;;
        PROCESSING)
            if [[ $age -gt $DISPATCHER_STATE_PROCESSING_STALE_SECONDS ]]; then
                log_error "RED: dispatcher state=PROCESSING for ${age}s (threshold: ${DISPATCHER_STATE_PROCESSING_STALE_SECONDS}s) — possibly frozen mid-inference"
                return 2
            fi
            log_info "Dispatcher state=PROCESSING OK: ${age}s old (threshold: ${DISPATCHER_STATE_PROCESSING_STALE_SECONDS}s) — long inference allowed"
            return 0
            ;;
        WINDING_DOWN)
            log_info "Dispatcher state=WINDING_DOWN — session ending gracefully, skipping heartbeat check"
            return 0  # Healthy: do not restart
            ;;
        DEAD)
            log_error "RED: dispatcher state=DEAD — session exited, restart required"
            return 2
            ;;
        *)
            log_warn "Dispatcher state='$state' unknown — falling back to heartbeat check"
            return 3  # SKIP
            ;;
    esac
}
