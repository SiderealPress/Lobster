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

    local state updated_at
    state=$(python3 -c "
import json, sys
try:
    d = json.load(open('$DISPATCHER_STATE_FILE'))
    print(d.get('state', 'unknown'))
except Exception as e:
    print('unknown')
" 2>/dev/null)

    updated_at=$(python3 -c "
import json, sys
try:
    d = json.load(open('$DISPATCHER_STATE_FILE'))
    print(d.get('updated_at', ''))
except Exception:
    print('')
" 2>/dev/null)

    if [[ -z "$state" ]]; then
        log_info "Dispatcher state: unreadable — skipping state-machine check"
        return 3  # SKIP
    fi

    # Calculate age of the state in seconds
    local age=0
    if [[ -n "$updated_at" ]]; then
        local updated_epoch
        updated_epoch=$(python3 -c "
from datetime import datetime, timezone
try:
    dt = datetime.fromisoformat('$updated_at')
    print(int(dt.timestamp()))
except Exception:
    print(0)
" 2>/dev/null)
        if [[ -n "$updated_epoch" && "$updated_epoch" != "0" ]]; then
            local now
            now=$(date +%s)
            age=$(( now - updated_epoch ))
        fi
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
