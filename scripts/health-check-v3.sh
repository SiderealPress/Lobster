#!/bin/bash
#===============================================================================
# Lobster Health Check v3 - Lifecycle-Aware, Deterministic Monitoring
#
# Design principles:
#   - Zero LLM dependency: no heartbeat, no tmux scraping, no MCP checks
#   - Lifecycle-aware: reads lobster-state.json to understand current phase
#   - Single observable truth: is the inbox draining?
#   - Recovery via systemd: never manually rebuild tmux sessions
#   - Direct Telegram alerts: curl, not outbox (outbox may be broken too)
#   - Low noise: only alert on genuine problems, not routine transitions
#
# Lifecycle states (from claude-persistent.sh):
#   active     - Claude is running (WAITING on wait_for_messages or PROCESSING)
#   starting   - Wrapper is launching Claude (transient, < 30s)
#   restarting - Wrapper is restarting after an exit (transient, < 60s)
#   hibernate  - DEPRECATED: dispatcher no longer writes this state (PR #1447).
#                If seen in a stale state file, treated as active (full checks apply).
#   backoff    - Wrapper hit rapid-restart limit, cooling down
#   stopped    - Wrapper received signal, shutting down
#   waking     - Wrapper detected messages, about to launch Claude
#
# Compaction suppression (inbox drain only):
#   When Claude Code compacts its context, tool calls pause for 1-3+ minutes.
#   During this window real user messages can age past STALE_THRESHOLD_SECONDS
#   and trigger a false-positive restart. To prevent this, on-compact.py writes
#   a compacted_at timestamp to lobster-state.json, and the stale-inbox check
#   is skipped for COMPACTION_SUPPRESS_SECONDS after that timestamp.
#   NOTE: Dispatcher liveness check (check_dispatcher_heartbeat) is NOT
#   suppressed during compaction — the 20-minute threshold covers it naturally.
#
# Dispatcher liveness (replaces WFM freshness + catchup suppression):
#   hooks/thinking-heartbeat.py writes a Unix epoch timestamp to
#   ~/lobster-workspace/logs/dispatcher-heartbeat on every PostToolUse event.
#   check_dispatcher_heartbeat() reads this single file and checks its age.
#   The 1200s threshold covers compaction, catchup, and boot without any
#   suppression logic. The dispatcher no longer needs to call
#   record-catchup-state.sh to suppress false alarms. See issue #1483.
#
# Boot grace period:
#   After any restart (health-check-initiated or manual), the new Claude session
#   needs ~60-90s to initialize and begin draining the inbox. During this window
#   the health-check skips stale-inbox and process/tmux checks to avoid
#   false-positive restarts. The boot timestamp is written to lobster-state.json
#   as booted_at by claude-persistent.sh (on first start) and by do_restart()
#   (after each health-check-initiated restart). Resource checks (memory, disk,
#   auth, outbox) still run during the grace period.
#   NOTE: Dispatcher heartbeat check is NOT suppressed during boot grace — the
#   20-minute threshold absorbs the 90s boot window naturally.
#
# Escalation ladder:
#   GREEN  - All checks pass (or in expected transient state)
#   YELLOW - Inbox messages exist < STALE threshold, or transient state
#   RED    - Stale inbox > threshold OR missing process/tmux/service -> restart
#   BLACK  - 3 restart failures in cooldown window -> alert, stop retrying
#
# Run via cron every 4 minutes:
#   */4 * * * * $HOME/lobster/scripts/health-check-v3.sh
#===============================================================================

set -o pipefail

#===============================================================================
# Configuration - single source of truth
#===============================================================================
TMUX_SOCKET="lobster"
TMUX_SESSION="lobster"
SERVICE_CLAUDE="lobster-claude"
SERVICE_ROUTER="lobster-router"

MESSAGES_DIR="${LOBSTER_MESSAGES:-$HOME/messages}"
WORKSPACE_DIR="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"

INBOX_DIR="$MESSAGES_DIR/inbox"
MAINTENANCE_FLAG="$MESSAGES_DIR/config/lobster-maintenance"
LOBSTER_STATE_FILE="${LOBSTER_STATE_FILE_OVERRIDE:-$MESSAGES_DIR/config/lobster-state.json}"
DISPATCHER_PID_FILE="$MESSAGES_DIR/config/dispatcher.pid"
STALE_THRESHOLD_SECONDS=360          # 6 minutes - RED if any message older; triggers restart (issue #1633)
# YELLOW_THRESHOLD_SECONDS: early-warning signal only — intentionally lower than
# STALE_THRESHOLD_SECONDS. Does not trigger restarts; fires at 2.5 min as early warning before 6-min restart.
YELLOW_THRESHOLD_SECONDS=150         # 2.5 minutes - YELLOW early warning before 6-min stale restart
RESTART_WINDOW_BUFFER_SECONDS=120    # Pre-mark messages within this window of the stale threshold before a restart


COMPACTION_SUPPRESS_SECONDS=300      # 5 minutes - skip stale-inbox check after a compaction event
COMPACT_GRACE_SECONDS=600            # 10 minutes - skip stale-inbox check after a compaction (last-compact.ts)
# CATCHUP_SUPPRESS_SECONDS removed (issue #1483): dispatcher heartbeat threshold covers catchup naturally
RESTART_COOLDOWN_SUPPRESS_SECONDS=240 # 4 minutes - suppress stale-inbox RED after a recent restart

BOOT_GRACE_SECONDS=90                # 90s - skip stale-inbox, WFM, and process checks after a restart

HIBERNATE_FRESH_SECONDS=30           # DEPRECATED — kept for reference; hibernate state is no longer written by dispatcher

WFM_STALE_SECONDS=1200               # 20 minutes - kept for backward-compat references; superseded by DISPATCHER_HEARTBEAT_STALE_SECONDS
HEARTBEAT_FILE="$WORKSPACE_DIR/logs/claude-heartbeat"   # legacy WFM-touch signal; superseded by dispatcher-heartbeat

# Dispatcher heartbeat sentinel (issue #1483 simplification)
# Written by hooks/thinking-heartbeat.py on every PostToolUse event.
# Single file, single integer (Unix epoch seconds). No JSON parsing required.
# Threshold is generous enough to cover compaction + catchup without suppression.
DISPATCHER_HEARTBEAT_FILE="${LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE:-$WORKSPACE_DIR/logs/dispatcher-heartbeat}"
DISPATCHER_HEARTBEAT_STALE_SECONDS=1200   # 20 min — covers compaction (~5m) + catchup (~12m) + margin

# WFM-active signal (issue #949): inbox_server.py writes this file with a Unix
# epoch timestamp when wait_for_messages begins blocking and refreshes it every
# 60s (WAIT_HEARTBEAT_INTERVAL). When this file is fresh, the dispatcher is alive
# and waiting for messages — heartbeat staleness does NOT indicate a problem.
# Threshold: 3x WAIT_HEARTBEAT_INTERVAL (180s) to absorb one missed refresh.
# File is deleted by the MCP server when WFM returns (message arrived or timeout).
DISPATCHER_WFM_ACTIVE_FILE="${LOBSTER_WFM_ACTIVE_OVERRIDE:-$WORKSPACE_DIR/logs/dispatcher-wfm-active}"
# 3x WAIT_HEARTBEAT_INTERVAL (60s) — absorbs one missed tick
WFM_ACTIVE_STALE_SECONDS=180

OUTBOX_DIR="$MESSAGES_DIR/outbox"
OUTBOX_STALE_THRESHOLD_SECONDS=900   # 15 min = RED
OUTBOX_YELLOW_THRESHOLD_SECONDS=300  # 5 min = YELLOW
OUTBOX_HISTORICAL_CUTOFF=3600        # Skip files > 1 hour (dead-letter candidates)

LOG_FILE="$WORKSPACE_DIR/logs/health-check.log"
LOCK_FILE="${LOBSTER_HEALTH_LOCK:-/tmp/lobster-health-check-v3.lock}"

MAX_RESTART_ATTEMPTS=3
RESTART_COOLDOWN_SECONDS=600         # 10 min window for counting attempts
RESTART_STATE_FILE="$WORKSPACE_DIR/logs/health-restart-state-v3"

BLACK_RENOTIFY_SECONDS=7200          # 2 hours: silent restart retry interval while in BLACK state

ALERT_DEDUP_COOLDOWN_SECONDS=900     # 15 minutes between alerts for the same issue type
ALERT_DEDUP_DIR="$WORKSPACE_DIR/logs/health-alert-dedup"

MEMORY_THRESHOLD=90                  # percentage
DISK_THRESHOLD=95                    # percentage

# User-facing message sources (only these count for inbox staleness)
USER_FACING_SOURCES="telegram sms signal slack"

# Genuine user-originated message types (only these count for inbox staleness).
# Internal queue messages like subagent_result/subagent_error may carry
# source="telegram" for routing purposes but are NOT user messages and must
# not trigger stale-inbox alerts or restarts.
USER_FACING_TYPES="message photo image voice audio callback text document"

# Circuit breaker: tracks which stale files already triggered a restart
# to prevent restart loops when the same message persists after restart
STALE_INBOX_MARKER_DIR="$WORKSPACE_DIR/logs/stale-inbox-markers"

# Telegram direct alerting (bypasses outbox entirely)
CONFIG_ENV="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"

# Read LOBSTER_DEBUG from config.env (if not already in environment)
if [[ -z "${LOBSTER_DEBUG:-}" && -f "$CONFIG_ENV" ]]; then
    LOBSTER_DEBUG=$(grep '^LOBSTER_DEBUG=' "$CONFIG_ENV" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]"' || echo "false")
fi
LOBSTER_DEBUG="${LOBSTER_DEBUG:-false}"

# Read LOBSTER_ENV from config.env (if not already in environment)
if [[ -z "${LOBSTER_ENV:-}" && -f "$CONFIG_ENV" ]]; then
    LOBSTER_ENV=$(grep '^LOBSTER_ENV=' "$CONFIG_ENV" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]"' || echo "production")
fi
LOBSTER_ENV="${LOBSTER_ENV:-production}"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$RESTART_STATE_FILE")"
mkdir -p "$ALERT_DEDUP_DIR"

# Dry-run gate: skip all real actions when LOBSTER_HEALTH_CHECK_DRY_RUN=1.
# Used by tests to exercise parsing/reading logic without executing systemctl,
# curl, or other external commands.
if [[ "${LOBSTER_HEALTH_CHECK_DRY_RUN:-0}" == "1" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$(date -Iseconds)] [INFO] LOBSTER_HEALTH_CHECK_DRY_RUN=1 — health check dry-run, skipping all actions" >> "$LOG_FILE"
    exit 0
fi

# Lifecycle gate: skip monitoring and restart loop in non-production environments.
# Resource checks (disk/memory/auth) do not run either — the service is intentionally
# idle and alerting on its resource state would be noise. The cron entry still fires
# so that flipping back to production takes effect within 4 minutes with no manual step.
if [[ "$LOBSTER_ENV" != "production" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$(date -Iseconds)] [INFO] LOBSTER_ENV=$LOBSTER_ENV — health check skipped in non-production mode" >> "$LOG_FILE"
    exit 0
fi

#===============================================================================
# Logging
#===============================================================================
log() {
    echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"
}
log_info()  { log "INFO"  "$1"; }
log_warn()  { log "WARN"  "$1"; }
log_error() { log "ERROR" "$1"; }

#===============================================================================
# Locking - prevent concurrent health checks
#===============================================================================
acquire_lock() {
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        exit 0
    fi
}

#===============================================================================
# Direct Telegram Alert (no LLM, no outbox, no MCP)
#===============================================================================
send_telegram_alert() {
    local message="$1"

    # Source config.env for bot token and user ID
    local bot_token=""
    local chat_id=""

    if [[ -f "$CONFIG_ENV" ]]; then
        bot_token=$(grep '^TELEGRAM_BOT_TOKEN=' "$CONFIG_ENV" | cut -d'=' -f2-)
        chat_id=$(grep '^TELEGRAM_ALLOWED_USERS=' "$CONFIG_ENV" | cut -d'=' -f2- | cut -d',' -f1)
    fi

    if [[ -z "$bot_token" || -z "$chat_id" ]]; then
        log_error "Cannot send Telegram alert: missing bot token or chat ID"
        return 1
    fi

    local full_message=$'🚨 *Lobster Health Alert*\n\n'"${message}"$'\n\n_'"$(date '+%Y-%m-%d %H:%M:%S %Z')"$'_'

    curl -s -X POST \
        "https://api.telegram.org/bot${bot_token}/sendMessage" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${full_message}" \
        --data-urlencode "parse_mode=Markdown" \
        --max-time 10 \
        > /dev/null 2>&1

    local rc=$?
    if [[ $rc -eq 0 ]]; then
        log_info "Telegram alert sent to $chat_id"
    else
        log_error "Telegram alert failed (curl exit $rc)"
    fi
}

# send_telegram_alert_deduped — like send_telegram_alert but suppresses repeat
# alerts for the same issue_key within ALERT_DEDUP_COOLDOWN_SECONDS.
#
# Usage:
#   send_telegram_alert_deduped "issue_key" "message text"
#
# The issue_key is a short stable identifier for the problem category
# (e.g. "stale-inbox", "wrapper-missing", "auth-expired").  Alerts with the
# same key are suppressed if a previous alert with that key was sent within
# ALERT_DEDUP_COOLDOWN_SECONDS.  This prevents a restart storm from flooding
# Telegram with dozens of identical alerts every 4 minutes.
#
# BLACK-state alerts and post-restart recovery confirmations should use the
# raw send_telegram_alert() to guarantee delivery regardless of cooldown.
send_telegram_alert_deduped() {
    local issue_key="$1"
    local message="$2"
    local now
    now=$(date +%s)

    # Sanitize the key to a safe filename (alphanumeric + hyphen)
    local safe_key
    safe_key=$(echo "$issue_key" | tr -cs 'a-zA-Z0-9-' '-' | sed 's/-\+/-/g; s/^-//; s/-$//')
    local stamp_file="$ALERT_DEDUP_DIR/${safe_key}"

    if [[ -f "$stamp_file" ]]; then
        local last_sent
        last_sent=$(cat "$stamp_file" 2>/dev/null || echo 0)
        local age=$(( now - last_sent ))
        if [[ $age -lt $ALERT_DEDUP_COOLDOWN_SECONDS ]]; then
            log_info "Alert dedup: suppressing '$issue_key' alert (sent ${age}s ago, cooldown ${ALERT_DEDUP_COOLDOWN_SECONDS}s)"
            return 0
        fi
    fi

    # Record send time before the curl call to avoid double-sends on retry
    echo "$now" > "$stamp_file"
    send_telegram_alert "$message"
}

#===============================================================================
# Restart Rate Limiting
#===============================================================================

# Check if manual intervention flag is set (BLACK state was reached).
# Returns 0 if flag is set, 1 if not.
is_manual_intervention_required() {
    [[ ! -f "$RESTART_STATE_FILE" ]] && return 1
    local line
    line=$(cat "$RESTART_STATE_FILE" 2>/dev/null || true)
    [[ "$line" == *"MANUAL_INTERVENTION"* ]]
}

# Write manual intervention flag into the state file.
# Preserves existing timestamp/count so the record is self-documenting.
# Appends MANUAL_INTERVENTION and the timestamp when BLACK was first set.
# Format: <first_restart_ts> <count> MANUAL_INTERVENTION <black_set_ts>
set_manual_intervention() {
    local now
    now=$(date +%s)
    local existing=""
    [[ -f "$RESTART_STATE_FILE" ]] && existing=$(cat "$RESTART_STATE_FILE" 2>/dev/null || true)
    # Strip any previous MANUAL_INTERVENTION token (and trailing black_set_ts), then append
    existing=$(echo "$existing" | sed 's/ MANUAL_INTERVENTION.*//')
    echo "${existing:-$now 0} MANUAL_INTERVENTION $now" > "$RESTART_STATE_FILE"
    log_warn "Manual intervention flag set in $RESTART_STATE_FILE"
}

# Return the epoch when BLACK was first set, or empty string if not available.
# Reads the 4th field from the state file (black_set_ts).
# Handles both old 3-field format (no black_set_ts) and new 4-field format.
get_black_set_ts() {
    [[ ! -f "$RESTART_STATE_FILE" ]] && return
    local line
    line=$(cat "$RESTART_STATE_FILE" 2>/dev/null || true)
    # Fields: <first_restart_ts> <count> MANUAL_INTERVENTION [<black_set_ts>]
    local black_set_ts
    black_set_ts=$(echo "$line" | awk '{print $4}')
    echo "$black_set_ts"
}

# If system has been in BLACK state longer than BLACK_RENOTIFY_SECONDS,
# silently attempt a single restart (no Telegram alert). If the restart
# succeeds the system will return to GREEN on the next health check and
# clear_manual_intervention() will remove the BLACK flag automatically.
# If the restart fails, re-set the BLACK flag and reset the 2-hour timer
# so another attempt fires BLACK_RENOTIFY_SECONDS from now.
#
# The one-time alert sent when BLACK is first set (in do_restart) is
# intentionally preserved. Only the periodic re-notifications are replaced
# by these silent retry attempts.
check_and_renotify_black() {
    local reason="${1:-periodic BLACK retry}"
    local black_set_ts
    black_set_ts=$(get_black_set_ts)
    if [[ -z "$black_set_ts" ]]; then
        # Old state file format without black_set_ts — update in place to add it now
        local line
        line=$(cat "$RESTART_STATE_FILE" 2>/dev/null || true)
        local base
        base=$(echo "$line" | sed 's/ MANUAL_INTERVENTION.*//')
        local now
        now=$(date +%s)
        echo "${base} MANUAL_INTERVENTION $now" > "$RESTART_STATE_FILE"
        log_info "BLACK: Migrated state file to include black_set_ts ($now)"
        return
    fi

    local now
    now=$(date +%s)
    local elapsed=$(( now - black_set_ts ))

    if [[ $elapsed -gt $BLACK_RENOTIFY_SECONDS ]]; then
        local hours=$(( elapsed / 3600 ))
        log_warn "BLACK: System in manual intervention state for ${hours}h — attempting silent restart (no alert)"
        # Temporarily clear the MANUAL_INTERVENTION flag so do_restart's
        # can_restart() check passes, then attempt a single restart.
        clear_manual_intervention
        if do_restart "$reason" "true"; then
            # Restart succeeded: the system will return to GREEN on the next
            # health check run, which will call clear_manual_intervention()
            # again (harmless no-op if already cleared). Nothing more to do.
            log_info "BLACK: Silent restart attempt succeeded — system should return to GREEN"
        else
            # Restart failed: re-enter BLACK and reset the 2-hour retry timer
            # so we try again BLACK_RENOTIFY_SECONDS from now.
            log_error "BLACK: Silent restart attempt failed — re-entering BLACK state"
            set_manual_intervention
            log_info "BLACK: Reset retry timer (next attempt in ${BLACK_RENOTIFY_SECONDS}s)"
        fi
    else
        local remaining=$(( BLACK_RENOTIFY_SECONDS - elapsed ))
        log_error "BLACK: Manual intervention required (flag already set, ${elapsed}s elapsed, retry in ${remaining}s) — skipping restart"
    fi
}

# Clear the manual intervention flag when the system is healthy again.
clear_manual_intervention() {
    if is_manual_intervention_required; then
        # Strip MANUAL_INTERVENTION and any trailing black_set_ts, keeping timestamp/count
        local line
        line=$(cat "$RESTART_STATE_FILE" 2>/dev/null || true)
        echo "$(echo "$line" | sed 's/ MANUAL_INTERVENTION.*//')" > "$RESTART_STATE_FILE"
        log_info "Manual intervention flag cleared (system healthy)"
    fi
}

can_restart() {
    if [[ ! -f "$RESTART_STATE_FILE" ]]; then
        return 0
    fi

    # If BLACK state was previously reached, refuse all auto-restarts until
    # the system recovers healthy and clears the flag.
    if is_manual_intervention_required; then
        return 1
    fi

    read -r last_restart_time restart_count _ < "$RESTART_STATE_FILE" 2>/dev/null || return 0
    local now
    now=$(date +%s)
    local elapsed=$((now - last_restart_time))

    # Reset counter if cooldown has fully passed
    if [[ $elapsed -gt $RESTART_COOLDOWN_SECONDS ]]; then
        return 0
    fi

    # Check if we've exceeded max attempts within the window
    if [[ $restart_count -ge $MAX_RESTART_ATTEMPTS ]]; then
        return 1
    fi

    return 0
}

record_restart() {
    local now
    now=$(date +%s)
    local restart_count=0

    if [[ -f "$RESTART_STATE_FILE" ]]; then
        read -r last_restart_time restart_count _ < "$RESTART_STATE_FILE" 2>/dev/null
        local elapsed=$((now - last_restart_time))
        if [[ $elapsed -gt $RESTART_COOLDOWN_SECONDS ]]; then
            restart_count=0
        fi
    fi

    restart_count=$((restart_count + 1))
    echo "$now $restart_count" > "$RESTART_STATE_FILE"
}

#===============================================================================
# Lifecycle State Check
#===============================================================================

# Read the current Lobster mode from state file.
# Returns one of: active, starting, restarting, backoff, stopped, waking, unknown
# May also return "hibernate" from stale state files — treated as active by check_claude_lifecycle()
read_lobster_mode() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        echo "unknown"
        return
    fi
    uv run python3 -c "
import json, sys
try:
    d = json.load(open('$LOBSTER_STATE_FILE'))
    print(d.get('mode', 'unknown'))
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

# Read state file age in seconds
read_state_age() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        echo "999999"
        return
    fi
    local file_time
    file_time=$(stat -c %Y "$LOBSTER_STATE_FILE" 2>/dev/null)
    if [[ -z "$file_time" ]]; then
        echo "999999"
        return
    fi
    local now
    now=$(date +%s)
    echo $((now - file_time))
}

is_hibernating() {
    # DEPRECATED: dispatcher no longer writes mode=hibernate (PR #1447).
    # Always returns false — hibernation suppression is no longer applied.
    return 1
}

# Check if a context compaction occurred within the last COMPACTION_SUPPRESS_SECONDS.
# Returns 0 (true) if inbox staleness checks should be suppressed, 1 otherwise.
# Reads compacted_at from lobster-state.json (written by hooks/on-compact.py).
is_compaction_recent() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        return 1
    fi
    local compacted_at
    compacted_at=$(uv run python3 -c "
import json, sys
try:
    d = json.load(open('$LOBSTER_STATE_FILE'))
    print(d.get('compacted_at', ''))
except Exception:
    print('')
" 2>/dev/null)
    if [[ -z "$compacted_at" ]]; then
        return 1
    fi
    local compacted_epoch
    compacted_epoch=$(date -d "$compacted_at" +%s 2>/dev/null) || return 1
    local now
    now=$(date +%s)
    local age=$((now - compacted_epoch))
    if [[ $age -le $COMPACTION_SUPPRESS_SECONDS ]]; then
        log_info "Recent compaction ${age}s ago (threshold: ${COMPACTION_SUPPRESS_SECONDS}s) — stale-inbox check suppressed"
        return 0
    fi
    return 1
}

# Check if a context compaction occurred within the last COMPACT_GRACE_SECONDS.
# Returns 0 (true) if inbox staleness checks should be suppressed, 1 otherwise.
# Reads the Unix timestamp from last-compact.ts (written by hooks/on-compact.py).
# This provides a 10-minute grace period for post-compaction re-orientation,
# extending the existing COMPACTION_SUPPRESS_SECONDS (5 min) window by an additional
# 5 minutes to cover cases where re-orientation takes longer than expected.
is_compact_grace_period() {
    local ts_file="$WORKSPACE_DIR/data/last-compact.ts"
    if [[ ! -f "$ts_file" ]]; then
        return 1
    fi
    local compact_ts
    compact_ts=$(cat "$ts_file" 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$compact_ts" ]] || ! [[ "$compact_ts" =~ ^[0-9]+$ ]]; then
        return 1
    fi
    local now
    now=$(date +%s)
    local age=$((now - compact_ts))
    if [[ $age -le $COMPACT_GRACE_SECONDS ]]; then
        log_info "Post-compaction grace period: compaction ${age}s ago (threshold: ${COMPACT_GRACE_SECONDS}s) — stale-inbox check suppressed"
        return 0
    fi
    return 1
}

# is_catchup_active() removed (issue #1483).
# The dispatcher heartbeat threshold (DISPATCHER_HEARTBEAT_STALE_SECONDS = 1200s)
# covers catchup duration naturally. No per-catchup suppression needed.
# The dispatcher no longer needs to call record-catchup-state.sh.

# Check if a boot/restart occurred within the last BOOT_GRACE_SECONDS.
# Returns 0 (true) if we are inside the grace window, 1 otherwise.
# Reads booted_at from lobster-state.json (written by claude-persistent.sh on
# first start and by do_restart() after a health-check-initiated restart).
is_boot_grace_period() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        return 1
    fi
    local booted_at
    booted_at=$(uv run python3 -c "
import json, sys
try:
    d = json.load(open('$LOBSTER_STATE_FILE'))
    print(d.get('booted_at', ''))
except Exception:
    print('')
" 2>/dev/null)
    if [[ -z "$booted_at" ]]; then
        return 1
    fi
    local booted_epoch
    booted_epoch=$(date -d "$booted_at" +%s 2>/dev/null) || return 1
    local now
    now=$(date +%s)
    local age=$((now - booted_epoch))
    if [[ $age -le $BOOT_GRACE_SECONDS ]]; then
        log_info "Boot grace period active: booted ${age}s ago (grace window: ${BOOT_GRACE_SECONDS}s) — skipping inbox/process/WFM checks"
        return 0
    fi
    return 1
}

# Check if a health-check-triggered restart occurred within the last
# RESTART_COOLDOWN_SUPPRESS_SECONDS. When Lobster restarts, the MCP server
# recovers stale messages from processing/ back to inbox/. Without this guard,
# the next health check run sees those recovered messages as new stale messages
# and fires another restart — a restart-triggered restart loop.
#
# Returns 0 (true) if inbox-stale RED should be suppressed, 1 otherwise.
is_recent_restart() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        return 1
    fi
    local last_restart_at
    last_restart_at=$(jq -r '.last_restart_at // empty' "$LOBSTER_STATE_FILE" 2>/dev/null)
    if [[ -z "$last_restart_at" ]]; then
        return 1
    fi
    local restart_epoch
    restart_epoch=$(date -d "$last_restart_at" +%s 2>/dev/null) || return 1
    local now
    now=$(date +%s)
    local age=$((now - restart_epoch))
    if [[ $age -le $RESTART_COOLDOWN_SUPPRESS_SECONDS ]]; then
        log_info "Recent restart ${age}s ago (cooldown: ${RESTART_COOLDOWN_SUPPRESS_SECONDS}s) — stale-inbox RED suppressed"
        return 0
    fi
    return 1
}

# Write booted_at timestamp into lobster-state.json without clobbering other fields.
# Called by do_restart() after a successful health-check-initiated restart.
write_boot_timestamp() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        return
    fi
    local now
    now=$(date -Iseconds)
    uv run python3 -c "
import json, sys
path = '$LOBSTER_STATE_FILE'
now = '$now'
try:
    with open(path) as f:
        d = json.load(f)
except Exception:
    d = {}
d['booted_at'] = now
with open(path, 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
" 2>/dev/null || true
    log_info "Boot timestamp written to state file (booted_at=$now)"
}

# Write the current time as last_restart_at into lobster-state.json.
# Called by do_restart() just before triggering the systemd restart so the
# post-restart health check can suppress false-positive stale-inbox REDs.
write_last_restart_at() {
    if [[ ! -f "$LOBSTER_STATE_FILE" ]]; then
        log_warn "write_last_restart_at: state file not found, skipping"
        return 0
    fi
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local tmp
    tmp=$(mktemp)
    if jq --arg ts "$ts" '.last_restart_at = $ts' "$LOBSTER_STATE_FILE" > "$tmp" 2>/dev/null; then
        mv "$tmp" "$LOBSTER_STATE_FILE"
        log_info "Wrote last_restart_at=$ts to state file"
    else
        rm -f "$tmp"
        log_warn "write_last_restart_at: jq failed to update state file"
    fi
}

# Check if the wrapper script (claude-persistent.sh) is running in tmux.
# The wrapper manages Claude's lifecycle, so if it's running, the system
# is operational even if Claude is temporarily absent (between restarts,
# hibernating, etc.)
check_wrapper_process() {
    local wrapper_pids
    wrapper_pids=$(pgrep -f "claude-persistent.sh" 2>/dev/null)

    if [[ -z "$wrapper_pids" ]]; then
        return 1
    fi

    # Verify at least one wrapper is in the lobster tmux.
    # Use -a to scan all sessions and windows, not just the default window.
    local tmux_panes
    tmux_panes=$(tmux -L "$TMUX_SOCKET" list-panes -a -F '#{pane_pid}' 2>/dev/null)
    [[ -z "$tmux_panes" ]] && return 1

    for pid in $wrapper_pids; do
        local check_pid="$pid"
        for _ in 1 2 3 4 5 6; do
            if echo "$tmux_panes" | grep -qw "$check_pid"; then
                return 0
            fi
            check_pid=$(ps -o ppid= -p "$check_pid" 2>/dev/null | tr -d ' ')
            [[ -z "$check_pid" || "$check_pid" == "1" ]] && break
        done
    done

    return 1
}

# Transient states where Claude may not be running but everything is fine
is_transient_state() {
    local mode="$1"
    local age="$2"
    # Starting/restarting/waking are transient - allow up to 120s
    case "$mode" in
        starting|restarting|waking)
            [[ $age -lt 120 ]]
            return $?
            ;;
        backoff)
            # Backoff is expected - allow up to 300s (5 min)
            [[ $age -lt 300 ]]
            return $?
            ;;
        *)
            return 1
            ;;
    esac
}

#===============================================================================
# Health Checks - all deterministic, no LLM dependency
#===============================================================================

# Check 1: Are systemd services active?
check_services() {
    local failed=0

    if ! systemctl is-active --quiet "$SERVICE_CLAUDE" 2>/dev/null; then
        log_error "Service $SERVICE_CLAUDE is not active"
        failed=1
    fi

    local router_state
    router_state=$(systemctl is-active "$SERVICE_ROUTER" 2>/dev/null || true)
    case "$router_state" in
        active)
            # OK — router is running normally
            ;;
        activating|reloading|deactivating)
            # Transient state during router restart — not alarming
            log_info "Service $SERVICE_ROUTER is in transient state: $router_state"
            ;;
        failed)
            log_error "Service $SERVICE_ROUTER has failed"
            failed=1
            ;;
        *)
            # inactive, unknown, etc. — warn but don't trigger claude restart
            log_warn "Service $SERVICE_ROUTER is in unexpected state: $router_state"
            ;;
    esac

    return $failed
}

# Check 2: Does the tmux session exist?
check_tmux() {
    if tmux -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
        return 0
    else
        log_error "Tmux session '$TMUX_SESSION' on socket '$TMUX_SOCKET' not found"
        return 1
    fi
}

# Check 3: Is a Claude process running inside the lobster tmux?
check_claude_process() {
    local claude_pids
    # Claude's cmdline shows just "claude" (flags not preserved in /proc/cmdline),
    # so match the exact binary name. The tmux ancestry check below filters out
    # any non-Lobster Claude sessions (e.g., interactive SSH sessions).
    claude_pids=$(pgrep -x "claude" 2>/dev/null)

    if [[ -z "$claude_pids" ]]; then
        log_error "No Claude process found"
        return 1
    fi

    # Verify at least one Claude process is a descendant of the tmux session.
    # Use -a to scan all sessions and windows, not just the default window.
    local tmux_panes
    tmux_panes=$(tmux -L "$TMUX_SOCKET" list-panes -a -F '#{pane_pid}' 2>/dev/null)

    if [[ -z "$tmux_panes" ]]; then
        log_error "Cannot list tmux panes"
        return 1
    fi

    for pid in $claude_pids; do
        local check_pid="$pid"
        # Walk up to 6 levels of parent PIDs
        for _ in 1 2 3 4 5 6; do
            if echo "$tmux_panes" | grep -qw "$check_pid"; then
                log_info "Claude PID $pid is in lobster tmux (ancestor $check_pid matches pane)"
                return 0
            fi
            check_pid=$(ps -o ppid= -p "$check_pid" 2>/dev/null | tr -d ' ')
            [[ -z "$check_pid" || "$check_pid" == "1" ]] && break
        done
    done

    log_error "Claude process(es) found but none are in the lobster tmux session"
    return 1
}

# Check if a source is user-facing (should count toward inbox staleness)
is_user_facing_source() {
    local source="$1"
    local s
    for s in $USER_FACING_SOURCES; do
        [[ "$source" == "$s" ]] && return 0
    done
    return 1
}

# Check if a type is a genuine user-originated message type.
# Internal types (subagent_result, subagent_error, system, compact, etc.)
# must not count toward inbox staleness even when they carry source="telegram".
is_user_facing_type() {
    local type="$1"
    local t
    for t in $USER_FACING_TYPES; do
        [[ "$type" == "$t" ]] && return 0
    done
    return 1
}

# Check 4: Inbox drain - THE primary deterministic check
# Only counts genuine user-originated messages. Two filters are applied:
#   1. source must be user-facing (telegram, sms, signal, slack)
#   2. type must be a genuine user type (message, photo, image, voice, audio,
#      callback, text, document) — this excludes internal queue messages such as
#      subagent_result and subagent_error which carry source="telegram" for
#      routing but are not real user messages.
# System/internal/task-output messages are ignored - they may sit in the inbox
# legitimately without indicating a stuck system.
#
# Circuit breaker: if a stale file already triggered a restart (tracked via
# marker files), it is skipped to prevent restart loops.
#
# Returns: 0=GREEN, 1=YELLOW, 2=RED
check_inbox_drain() {
    local now
    now=$(date +%s)
    local oldest_age=0
    local stale_count=0
    local yellow_count=0
    local total_count=0
    local skipped_system=0
    local skipped_circuit_breaker=0

    while IFS= read -r -d '' f; do
        local basename_f
        basename_f=$(basename "$f")

        # Parse source and type from JSON using jq; skip if unparseable or missing
        local source type
        source=$(jq -r '.source // empty' "$f" 2>/dev/null)
        type=$(jq -r '.type // empty' "$f" 2>/dev/null)
        if [[ -z "$source" ]]; then
            log_info "Skipping $basename_f: cannot parse source field"
            continue
        fi

        # Only count user-facing sources
        if ! is_user_facing_source "$source"; then
            skipped_system=$((skipped_system + 1))
            continue
        fi

        # Exclude internal queue messages that carry a user-facing source for
        # routing purposes but are not genuine user messages (e.g. subagent_result
        # messages have source="telegram" but type="subagent_result").
        if [[ -n "$type" ]] && ! is_user_facing_type "$type"; then
            skipped_system=$((skipped_system + 1))
            log_info "Skipping $basename_f: internal message type '$type' (source=$source)"
            continue
        fi

        # Circuit breaker: skip files that already triggered a restart
        if [[ -d "$STALE_INBOX_MARKER_DIR" && -f "$STALE_INBOX_MARKER_DIR/$basename_f" ]]; then
            skipped_circuit_breaker=$((skipped_circuit_breaker + 1))
            log_info "Circuit breaker: skipping $basename_f (already triggered restart)"
            continue
        fi

        total_count=$((total_count + 1))
        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))
        [[ $age -gt $oldest_age ]] && oldest_age=$age

        if [[ $age -gt $STALE_THRESHOLD_SECONDS ]]; then
            stale_count=$((stale_count + 1))
        elif [[ $age -gt $YELLOW_THRESHOLD_SECONDS ]]; then
            yellow_count=$((yellow_count + 1))
        fi
    done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    if [[ $skipped_system -gt 0 ]]; then
        log_info "Inbox drain: skipped $skipped_system non-user message(s)"
    fi
    if [[ $skipped_circuit_breaker -gt 0 ]]; then
        log_info "Inbox drain: skipped $skipped_circuit_breaker circuit-breaker message(s)"
    fi

    if [[ $stale_count -gt 0 ]]; then
        log_error "RED: $stale_count user message(s) older than ${STALE_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 2
    elif [[ $yellow_count -gt 0 ]]; then
        log_warn "YELLOW: $yellow_count user message(s) older than ${YELLOW_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 1
    elif [[ $total_count -gt 0 ]]; then
        log_info "Inbox has $total_count user message(s), all fresh (oldest: ${oldest_age}s)"
        return 0
    else
        return 0
    fi
}

# Check 5: Outbox drain - are outgoing messages being delivered?
# Returns: 0=GREEN, 1=YELLOW, 2=RED
check_outbox_drain() {
    local now
    now=$(date +%s)
    local oldest_age=0
    local stale_count=0
    local yellow_count=0
    local total_count=0

    while IFS= read -r -d '' f; do
        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))

        # Skip historical stuck files (dead-letter candidates)
        [[ $age -gt $OUTBOX_HISTORICAL_CUTOFF ]] && continue

        total_count=$((total_count + 1))
        [[ $age -gt $oldest_age ]] && oldest_age=$age

        if [[ $age -gt $OUTBOX_STALE_THRESHOLD_SECONDS ]]; then
            stale_count=$((stale_count + 1))
        elif [[ $age -gt $OUTBOX_YELLOW_THRESHOLD_SECONDS ]]; then
            yellow_count=$((yellow_count + 1))
        fi
    done < <(find "$OUTBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)

    if [[ $stale_count -gt 0 ]]; then
        log_error "RED: $stale_count outbox message(s) older than ${OUTBOX_STALE_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 2
    elif [[ $yellow_count -gt 0 ]]; then
        log_warn "YELLOW: $yellow_count outbox message(s) older than ${OUTBOX_YELLOW_THRESHOLD_SECONDS}s (oldest: ${oldest_age}s)"
        return 1
    elif [[ $total_count -gt 0 ]]; then
        log_info "Outbox has $total_count message(s), all fresh (oldest: ${oldest_age}s)"
        return 0
    else
        return 0
    fi
}

# Check 6: Dispatcher heartbeat sentinel (issue #1483 simplification)
#
# The dispatcher is considered alive if hooks/thinking-heartbeat.py has written
# to DISPATCHER_HEARTBEAT_FILE within the last DISPATCHER_HEARTBEAT_STALE_SECONDS.
# The hook fires on every PostToolUse event — any tool call resets the clock.
#
# This single-file check replaces the previous multi-signal approach
# (claude-heartbeat file + last_processed_at + last_thinking_at in
# lobster-state.json). The 20-minute threshold naturally covers:
#   - Context compaction pause (1-3 minutes with no tool calls)
#   - Startup catchup subagent (up to 10-12 minutes)
#   - Boot grace period (60-90 seconds)
#
# No suppression logic needed — the threshold does the work.
#
# Gracefully skips the check if the heartbeat file does not exist (fresh install
# or first run before the hook has fired).
#
# Returns: 0=GREEN (fresh or skipped), 2=RED (stale)
check_dispatcher_heartbeat() {
    if [[ ! -f "$DISPATCHER_HEARTBEAT_FILE" ]]; then
        log_info "Dispatcher heartbeat: file not found — skipping check (fresh install?)"
        return 0
    fi

    local raw_ts
    raw_ts=$(cat "$DISPATCHER_HEARTBEAT_FILE" 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$raw_ts" ]] || ! [[ "$raw_ts" =~ ^[0-9]+$ ]]; then
        log_info "Dispatcher heartbeat: unreadable or non-integer content — skipping check"
        return 0
    fi

    local now age
    now=$(date +%s)
    age=$(( now - raw_ts ))

    if [[ $age -gt $DISPATCHER_HEARTBEAT_STALE_SECONDS ]]; then
        # Heartbeat is stale — check the WFM-active signal before declaring RED.
        # When the dispatcher is blocked in wait_for_messages, PostToolUse hooks
        # do not fire so the heartbeat goes stale. inbox_server.py writes
        # DISPATCHER_WFM_ACTIVE_FILE with a fresh epoch timestamp every 60s while
        # WFM is blocking. A fresh WFM-active file means the dispatcher is alive
        # and simply idle — not frozen or dead. (issue #949)
        local _wfm_file="${DISPATCHER_WFM_ACTIVE_FILE:-}"
        local wfm_active_ts=""
        if [[ -n "$_wfm_file" && -f "$_wfm_file" ]]; then
            wfm_active_ts=$(cat "$_wfm_file" 2>/dev/null | tr -d '[:space:]')
        fi
        if [[ -n "$wfm_active_ts" ]] && [[ "$wfm_active_ts" =~ ^[0-9]+$ ]]; then
            local wfm_age=$(( now - wfm_active_ts ))
            if [[ $wfm_age -le $WFM_ACTIVE_STALE_SECONDS ]]; then
                log_info "Dispatcher heartbeat stale (${age}s) but WFM-active is fresh (${wfm_age}s) — dispatcher alive in wait_for_messages, skipping RED"
                return 0
            else
                log_error "RED: dispatcher heartbeat stale (${age}s) and WFM-active also stale (${wfm_age}s, threshold: ${WFM_ACTIVE_STALE_SECONDS}s) — dispatcher appears frozen"
                return 2
            fi
        fi
        log_error "RED: dispatcher heartbeat stale — last tool use ${age}s ago (threshold: ${DISPATCHER_HEARTBEAT_STALE_SECONDS}s)"
        return 2
    fi

    log_info "Dispatcher heartbeat OK: last tool use ${age}s ago (threshold: ${DISPATCHER_HEARTBEAT_STALE_SECONDS}s)"
    return 0
}

# Check 7: Memory
check_memory() {
    local mem_pct
    mem_pct=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2 * 100}')

    if [[ $mem_pct -gt $MEMORY_THRESHOLD ]]; then
        log_error "Memory critical: ${mem_pct}% (threshold: ${MEMORY_THRESHOLD}%)"
        return 1
    fi

    log_info "Memory OK: ${mem_pct}%"
    return 0
}

# Check 8: Disk
check_disk() {
    local disk_pct
    disk_pct=$(df "$HOME" | awk 'NR==2 {gsub(/%/,""); print $5}')

    if [[ $disk_pct -gt $DISK_THRESHOLD ]]; then
        log_error "Disk critical: ${disk_pct}% (threshold: ${DISK_THRESHOLD}%)"
        return 1
    fi

    log_info "Disk OK: ${disk_pct}%"
    return 0
}

# Check 8: Claude auth token validity
# Uses `claude auth status` as the single source of truth.
#
# Auth is managed via CLAUDE_CODE_OAUTH_TOKEN env var in lobster-config/config.env.
# The token is passed directly to Claude Code — no credentials file is involved.
# `claude auth status` is the authoritative check regardless
# of how the token was provisioned.
#
# RESTART GUARD: When AUTH RED is detected, do NOT restart Claude — restarting
# cannot fix an auth problem and causes a crash loop. The auth_rc=2 check in
# main() is deliberately NOT wired to do_restart(). Instead, we send an alert
# and set YELLOW so the operator can intervene by updating CLAUDE_CODE_OAUTH_TOKEN
# in config.env.
#
# Returns: 0=GREEN, 1=YELLOW (transient failure), 2=RED (confirmed not logged in)
AUTH_FAILURE_COUNTER_FILE="$WORKSPACE_DIR/logs/auth-token-failures"
AUTH_CONSECUTIVE_RED_THRESHOLD=3  # Must fail this many consecutive 4-min checks (~12 min total)

check_auth_token() {
    # Single check: `claude auth status` is the authoritative source of truth.
    # Auth is managed via CLAUDE_CODE_OAUTH_TOKEN env var in config.env.
    # Unset CLAUDECODE/CLAUDE_CODE_ENTRYPOINT to avoid nested-session errors.
    #
    # NOTE: `claude auth status` outputs JSON by default. Do NOT pass
    # --output-format json — that flag does not exist and causes an error,
    # leaving auth_json empty and making the parse return "unknown" every run.
    local auth_json
    auth_json=$(env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT \
        claude auth status 2>/dev/null)

    local logged_in auth_method
    logged_in=$(echo "$auth_json" | uv run python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('true' if d.get('loggedIn') else 'false')
except:
    print('unknown')
" 2>/dev/null)
    auth_method=$(echo "$auth_json" | uv run python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('authMethod', 'unknown'))
except:
    print('unknown')
" 2>/dev/null)

    if [[ "$logged_in" == "false" ]]; then
        # Confirmed not logged in — increment consecutive counter to avoid
        # false positives from transient `claude auth status` failures.
        local failure_count=0
        if [[ -f "$AUTH_FAILURE_COUNTER_FILE" ]]; then
            failure_count=$(cat "$AUTH_FAILURE_COUNTER_FILE" 2>/dev/null || echo 0)
        fi
        failure_count=$((failure_count + 1))
        echo "$failure_count" > "$AUTH_FAILURE_COUNTER_FILE"
        log_error "AUTH RED: claude auth status reports loggedIn=false (consecutive: $failure_count/$AUTH_CONSECUTIVE_RED_THRESHOLD) — check CLAUDE_CODE_OAUTH_TOKEN in config.env"
        if [[ $failure_count -ge $AUTH_CONSECUTIVE_RED_THRESHOLD ]]; then
            rm -f "$AUTH_FAILURE_COUNTER_FILE"
            return 2
        else
            return 1
        fi
    elif [[ "$logged_in" == "unknown" ]]; then
        # Could not parse output — treat as transient and log a warning
        log_warn "AUTH YELLOW: could not parse 'claude auth status' output — treating as transient"
        return 1
    fi

    # Logged in — reset failure counter and report GREEN.
    rm -f "$AUTH_FAILURE_COUNTER_FILE"
    log_info "AUTH OK: loggedIn=true via $auth_method (CLAUDE_CODE_OAUTH_TOKEN)"
    return 0
}

# Check 9: Dashboard server - silently restart if not listening on port 9100
check_dashboard_server() {
    local install_dir="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
    local dashboard_cmd="$install_dir/.venv/bin/python3 $install_dir/src/dashboard/server.py --host 0.0.0.0 --port 9100"

    if ss -tlnp | grep -q 9100; then
        log_info "Dashboard server OK: listening on port 9100"
        return 0
    fi

    log_warn "Dashboard server not running on port 9100 - restarting"
    nohup $dashboard_cmd >> "$WORKSPACE_DIR/logs/dashboard-server.log" 2>&1 200>&- &
    log_info "Dashboard server restarted (PID $!)"
    return 0
}

# Check 11: messages.db liveness (BIS-167 Slice 6, advisory only - never RED)
# Reports DB size and WAL health when LOBSTER_USE_DB=1.
# Never escalates to RED: the DB is additive; JSON files remain source of truth.
check_messages_db() {
    local db_path="${LOBSTER_MESSAGES_DB:-$MESSAGES_DIR/messages.db}"

    # If DB feature flag is off, silently pass
    if [[ "${LOBSTER_USE_DB:-0}" != "1" ]]; then
        log_info "messages.db: LOBSTER_USE_DB not enabled — skipping DB check"
        return 0
    fi

    if [[ ! -f "$db_path" ]]; then
        log_warn "messages.db: not found at $db_path (DB writes enabled but file absent)"
        return 0  # Not RED — DB created on first write
    fi

    # Check DB integrity (fast check, < 1s on healthy DB)
    if ! sqlite3 "$db_path" "PRAGMA integrity_check;" 2>/dev/null | grep -q "^ok$"; then
        log_warn "messages.db: integrity_check failed at $db_path"
        return 0  # Advisory only — never RED
    fi

    local db_size_kb
    db_size_kb=$(du -k "$db_path" 2>/dev/null | cut -f1)
    local row_count
    row_count=$(sqlite3 "$db_path" "SELECT (SELECT COUNT(*) FROM messages) + (SELECT COUNT(*) FROM bisque_events) + (SELECT COUNT(*) FROM agent_events);" 2>/dev/null || echo "?")
    log_info "messages.db: OK — ${db_size_kb}K, ${row_count} total rows"
    return 0
}

# Check 10: Required cron entries
check_cron_entries() {
    local REQUIRED_CRON_MARKERS=("# LOBSTER-HEALTH")

    for marker in "${REQUIRED_CRON_MARKERS[@]}"; do
        if ! crontab -l 2>/dev/null | grep -qF "$marker"; then
            log_warn "Missing cron entry: $marker"
            # LOBSTER-HEALTH checking itself is circular (if we're running, the health cron is working).
            # Just warn; do not attempt auto-restore.
        else
            log_info "Cron entry present: $marker"
        fi
    done
}

#===============================================================================
# Circuit Breaker - prevent restart loops for persistent stale messages
#===============================================================================

# Record which inbox files triggered a stale-inbox restart.
# On the next health check, these files will be skipped by check_inbox_drain()
# so we don't restart again for the same stuck messages.
record_stale_inbox_markers() {
    mkdir -p "$STALE_INBOX_MARKER_DIR"
    # Clear old markers first
    rm -f "$STALE_INBOX_MARKER_DIR"/*.json 2>/dev/null

    local now
    now=$(date +%s)

    while IFS= read -r -d '' f; do
        local basename_f
        basename_f=$(basename "$f")
        local source type
        source=$(jq -r '.source // empty' "$f" 2>/dev/null)
        type=$(jq -r '.type // empty' "$f" 2>/dev/null)
        [[ -z "$source" ]] && continue
        is_user_facing_source "$source" || continue
        # Mirror the type filter from check_inbox_drain: skip internal messages
        if [[ -n "$type" ]] && ! is_user_facing_type "$type"; then
            continue
        fi

        local file_time
        file_time=$(stat -c %Y "$f" 2>/dev/null)
        [[ -z "$file_time" ]] && continue

        local age=$((now - file_time))
        local mark_threshold=$(( STALE_THRESHOLD_SECONDS - RESTART_WINDOW_BUFFER_SECONDS ))
        if [[ $age -gt $mark_threshold ]]; then
            touch "$STALE_INBOX_MARKER_DIR/$basename_f"
            if [[ $age -gt $STALE_THRESHOLD_SECONDS ]]; then
                log_info "Circuit breaker: marked $basename_f as restart-triggering (stale: ${age}s)"
            else
                log_info "Circuit breaker: pre-emptively marked $basename_f (near-threshold: ${age}s, within ${RESTART_WINDOW_BUFFER_SECONDS}s restart window)"
            fi
        fi
    done < <(find "$INBOX_DIR" -maxdepth 1 -name "*.json" -print0 2>/dev/null)
}

# Clear circuit breaker markers (called when inbox is healthy)
clear_stale_inbox_markers() {
    if [[ -d "$STALE_INBOX_MARKER_DIR" ]]; then
        rm -rf "$STALE_INBOX_MARKER_DIR"
    fi
}

#===============================================================================
# Subagent Guard - abort restart if active subagent sessions exist
#===============================================================================

# Returns the count of currently-running subagent sessions from agent_sessions.db.
# Returns 0 if the database doesn't exist or cannot be read (fail-open: allow restart).
count_active_subagents() {
    local db_file="${MESSAGES_DIR}/config/agent_sessions.db"
    if [[ ! -f "$db_file" ]]; then
        echo "0"
        return
    fi
    uv run python3 -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$db_file')
    row = conn.execute(\"SELECT COUNT(*) FROM agent_sessions WHERE status='running'\").fetchone()
    print(row[0] if row else 0)
except Exception as e:
    print(0)
" 2>/dev/null || echo "0"
}

#===============================================================================
# Recovery - always via systemd, never manual tmux
#===============================================================================
# DANGER: do_restart() must ONLY be called when the system is confirmed
# unhealthy (RED state). Calling it on a healthy system would unlink the tmux
# socket path (via the stale-socket cleanup below), preventing new client
# connections even while the server is still running. The safety of the socket
# cleanup relies entirely on this function being called only from the RED state.
#
# Parameters:
#   $1 reason        - human-readable reason for the restart
#   $2 suppress_alert - "true" to skip Telegram alerts (used during compaction
#                       window so exactly one notification fires per compaction:
#                       on-compact.py already notified the user)
do_restart() {
    local reason="$1"
    local suppress_alert="${2:-false}"
    log_warn "Restarting $SERVICE_CLAUDE (reason: $reason, suppress_alert=$suppress_alert)"

    # Subagent guard: do not restart while subagents are active.
    # A systemd restart kills the entire Claude process tree including all
    # sidechain agents. If the DB shows running sessions, log a warning and
    # abort — the subagent will complete and the next health-check run will
    # re-evaluate whether a restart is still needed.
    local active_subagents
    active_subagents=$(count_active_subagents)
    if [[ "$active_subagents" -gt 0 ]]; then
        log_warn "SUBAGENT GUARD: Skipping restart ($active_subagents active subagent(s) running) — will re-evaluate next check"
        if [[ "$suppress_alert" != "true" ]]; then
            send_telegram_alert_deduped "subagent-guard" "Health check deferred restart: $active_subagents active subagent(s) in flight.

Reason that triggered restart: $reason

The restart has been skipped to avoid killing running subagents. If the problem persists, the next health check will re-evaluate."
        else
            log_info "Telegram alert suppressed (compaction window active)"
        fi
        return 0
    fi

    if ! can_restart; then
        if is_manual_intervention_required; then
            # Already in BLACK/manual-intervention state — check whether it's time to re-alert.
            # check_and_renotify_black silently retries a restart if BLACK_RENOTIFY_SECONDS
            # (2h) have elapsed. No Telegram alert is sent — the initial BLACK alert already
            # fired. If the retry succeeds the system returns to GREEN naturally.
            check_and_renotify_black "$reason"
        else
            # First time hitting BLACK — set flag and send a single alert.
            # Alert fires even during compaction window: this is a severe state
            # that requires user action regardless of what triggered the restart.
            log_error "BLACK: Max restart attempts ($MAX_RESTART_ATTEMPTS) in ${RESTART_COOLDOWN_SECONDS}s window"
            set_manual_intervention
            send_telegram_alert "System unrecoverable after $MAX_RESTART_ATTEMPTS restart attempts.

Reason: $reason

Manual intervention required:
\`lobster restart\`"
        fi
        return 1
    fi

    # If restarting for stale inbox, record which files triggered it
    # so the circuit breaker can skip them on the next check
    if [[ "$reason" == *"stale inbox"* ]]; then
        record_stale_inbox_markers
    fi

    # Record the restart time in lobster-state.json so the next health check
    # run can suppress false-positive stale-inbox REDs caused by messages
    # recovered from processing/ back to inbox/ during the restart.
    write_last_restart_at

    record_restart

    # Capture the Claude PID before stopping, so we can verify a new process
    # is running after restart (not just the same surviving process), and so we
    # can kill it directly if ExecStop fails silently.
    #
    # Read from the PID file written by claude-persistent.sh at launch time.
    # This is unambiguous: pgrep -x "claude" could match an unrelated Claude
    # process (e.g. a debug session), but the PID file records exactly the
    # dispatcher that this health check is responsible for.
    local pre_restart_pid=""
    if [[ -f "$DISPATCHER_PID_FILE" ]]; then
        pre_restart_pid=$(< "$DISPATCHER_PID_FILE")
        # Sanity check: must be a non-empty integer
        if [[ ! "$pre_restart_pid" =~ ^[0-9]+$ ]]; then
            log_warn "dispatcher.pid contains non-numeric value '$pre_restart_pid' — ignoring"
            pre_restart_pid=""
        fi
    fi
    if [[ -n "$pre_restart_pid" ]]; then
        log_info "Pre-restart dispatcher PID: $pre_restart_pid (from $DISPATCHER_PID_FILE)"
    else
        log_warn "No dispatcher.pid found — pre-restart PID unknown (new install or first restart)"
    fi

    local tmux_uid
    tmux_uid=$(id -u)
    local stale_socket="/tmp/tmux-${tmux_uid}/${TMUX_SOCKET}"

    # Stop first while the tmux socket still exists.
    # ExecStop runs "tmux kill-session", which requires the socket to be present.
    # If we delete the socket before stopping, ExecStop fails silently and the
    # old Claude session survives alongside the new one started by ExecStart.
    sudo systemctl stop "$SERVICE_CLAUDE" 2>&1 | while read -r line; do
        log_info "systemctl stop: $line"
    done

    # Kill the tmux server directly — this terminates all sessions and their
    # child processes. Under RemainAfterExit=yes, systemctl stop marks the
    # service inactive but does not reliably kill the tmux server or the Claude
    # process tree. kill-server sends SIGTERM to every attached process and then
    # tears down the server, ensuring no orphan Claude process survives.
    if tmux -L "$TMUX_SOCKET" kill-server 2>/dev/null; then
        log_info "Tmux server on socket '$TMUX_SOCKET' killed successfully"
    else
        log_info "Tmux kill-server returned non-zero (socket may already be gone — OK)"
    fi

    # Belt-and-suspenders: if the pre-restart Claude PID is still alive after
    # systemctl stop + tmux kill-server, kill it explicitly. SIGTERM first,
    # then SIGKILL after a brief wait. This handles cases where the process
    # detached from the tmux session before the kill-server ran.
    #
    # Unlike pgrep, targeting the PID file is unambiguous: it records exactly the
    # dispatcher process launched by claude-persistent.sh, not any other Claude
    # process that may be running (e.g. a debug session or subagent).
    if [[ -n "$pre_restart_pid" ]] && kill -0 "$pre_restart_pid" 2>/dev/null; then
        log_warn "Claude PID $pre_restart_pid survived systemctl stop + tmux kill-server — sending SIGTERM"
        kill -TERM "$pre_restart_pid" 2>/dev/null || true
        sleep 2
        if kill -0 "$pre_restart_pid" 2>/dev/null; then
            log_warn "Claude PID $pre_restart_pid still alive after SIGTERM — sending SIGKILL"
            kill -KILL "$pre_restart_pid" 2>/dev/null || true
            sleep 1
        fi
    fi

    # Clean up the PID file — the process is gone (or was already gone).
    rm -f "$DISPATCHER_PID_FILE" 2>/dev/null || true

    # Verify the old process is actually dead before starting a new session.
    # If we cannot confirm it is gone, abort rather than risk two competing
    # dispatchers running simultaneously.
    if [[ -n "$pre_restart_pid" ]] && kill -0 "$pre_restart_pid" 2>/dev/null; then
        log_error "ABORT: Could not kill pre-restart Claude PID $pre_restart_pid — refusing to start new session to prevent duplicate dispatcher"
        send_telegram_alert_deduped "restart-aborted-pid" "Restart aborted — could not kill existing Claude process (PID $pre_restart_pid).

Reason: $reason

Manual intervention required to kill the process before restarting:
\`kill -9 $pre_restart_pid && lobster restart\`"
        return 1
    fi

    # Clean up the socket only if stop left it behind.
    # A successful ExecStop removes it via tmux kill-session; if stop failed or
    # the socket was already stale, we clean it up here so ExecStart can bind.
    if [[ -S "$stale_socket" ]]; then
        log_warn "Removing stale tmux socket after stop: $stale_socket"
        rm -f "$stale_socket"
    fi

    # Start fresh
    sudo systemctl start "$SERVICE_CLAUDE" 2>&1 | while read -r line; do
        log_info "systemctl start: $line"
    done

    # Wait for startup
    sleep 5

    # Verify recovery: service and tmux must be running, and the Claude PID
    # must differ from the pre-restart PID (catching ghost sessions where the
    # old process survived alongside the newly started one).
    #
    # PID retry loop: the new claude-persistent.sh may not have written the PID
    # file yet. Retry up to 3 times with 3-second gaps before concluding that
    # the PID is genuinely unchanged (i.e. restart failed).
    #
    # Read from the PID file (same source as pre_restart_pid) for a consistent
    # comparison. If the file is absent or empty, the new process hasn't written
    # it yet — treat as "not ready" and keep retrying.
    local post_restart_pid=""
    local pid_changed=true
    local pid_check_attempts=0
    while [[ $pid_check_attempts -lt 3 ]]; do
        if [[ -f "$DISPATCHER_PID_FILE" ]]; then
            post_restart_pid=$(< "$DISPATCHER_PID_FILE")
            [[ ! "$post_restart_pid" =~ ^[0-9]+$ ]] && post_restart_pid=""
        else
            post_restart_pid=""
        fi
        if [[ -z "$pre_restart_pid" || ( -n "$post_restart_pid" && "$post_restart_pid" != "$pre_restart_pid" ) ]]; then
            break
        fi
        pid_check_attempts=$(( pid_check_attempts + 1 ))
        if [[ $pid_check_attempts -lt 3 ]]; then
            log_info "PID unchanged after restart (attempt $pid_check_attempts/3), waiting 3s..."
            sleep 3
        fi
    done
    if [[ -n "$pre_restart_pid" && "$post_restart_pid" == "$pre_restart_pid" ]]; then
        pid_changed=false
        log_error "Restart verification failed: Claude PID $pre_restart_pid unchanged after 3 attempts — old session may have survived"
    fi

    # Service/tmux check with retry: systemd and tmux may still be initializing
    # when we first check. If the PID changed (restart succeeded), give the
    # service up to 15 seconds to reach active state before declaring failure.
    # This prevents the false-positive "PID unchanged" alert that fires when the
    # new process starts fine but the service status query races the activation.
    local service_ok=false
    local tmux_ok=false
    local svc_check_attempts=0
    local max_svc_attempts=5
    if [[ "$pid_changed" == true ]]; then
        while [[ $svc_check_attempts -lt $max_svc_attempts ]]; do
            service_ok=false
            tmux_ok=false
            if systemctl is-active --quiet "$SERVICE_CLAUDE" 2>/dev/null; then
                service_ok=true
            fi
            if tmux -L "$TMUX_SOCKET" has-session -t "$TMUX_SESSION" 2>/dev/null; then
                tmux_ok=true
            fi
            if [[ "$service_ok" == true && "$tmux_ok" == true ]]; then
                break
            fi
            log_info "Service/tmux not ready yet (attempt $(( svc_check_attempts + 1 ))/$max_svc_attempts, service_ok=$service_ok tmux_ok=$tmux_ok), waiting 3s..."
            svc_check_attempts=$(( svc_check_attempts + 1 ))
            if [[ $svc_check_attempts -lt $max_svc_attempts ]]; then
                sleep 3
            fi
        done
    fi

    if [[ "$pid_changed" == true && "$service_ok" == true && "$tmux_ok" == true ]]; then

        # For stale-inbox restarts, also re-verify inbox drain
        if [[ "$reason" == *"stale inbox"* ]]; then
            # Re-check inbox (circuit breaker markers will skip already-known files)
            check_inbox_drain
            local post_rc=$?
            if [[ $post_rc -eq 2 ]]; then
                log_warn "Post-restart: inbox still has NEW stale messages (not same as pre-restart)"
                send_telegram_alert_deduped "post-restart-stale-inbox" "System restarted but inbox still has stale messages.

Reason: $reason
Status: Restarted, but new stale messages detected post-restart"
                return 0
            fi
        fi

        log_info "Restart successful"
        write_boot_timestamp
        if [[ "$suppress_alert" != "true" ]]; then
            # "Recovered" alert: use raw send (important positive signal, not spammy)
            send_telegram_alert "System recovered automatically.

Reason: $reason
Status: Restarted successfully"
        else
            log_info "Post-restart Telegram alert suppressed (compaction window active)"
        fi
        return 0
    else
        local svc_timeout_s=$(( max_svc_attempts * 3 ))
        if [[ "$pid_changed" == false ]]; then
            # PID did not change: the old process survived the restart attempt.
            log_error "Restart failed: Claude PID $pre_restart_pid unchanged after 3 checks — old session survived"
            send_telegram_alert_deduped "restart-failed-pid" "Restart failed — process still running under original PID $pre_restart_pid.

Reason: $reason
Manual intervention may be required: \`lobster restart\`"
        else
            # PID changed (restart happened) but service/tmux not ready within the timeout window.
            local not_ready_detail
            if [[ "$service_ok" == false && "$tmux_ok" == false ]]; then
                not_ready_detail="service not active and tmux session missing"
            elif [[ "$service_ok" == false ]]; then
                not_ready_detail="service not active (tmux session exists)"
            else
                not_ready_detail="tmux session missing (service is active)"
            fi
            log_warn "Restart confirmed (PID changed) but service/tmux not ready after ${svc_timeout_s}s: $not_ready_detail"
            send_telegram_alert_deduped "restart-not-ready" "Restart confirmed (PID changed) — service/tmux not yet ready after ${svc_timeout_s}s.

Reason: $reason
Detail: $not_ready_detail
System may still be initializing. Check \`lobster status\` in a moment."
        fi
        return 1
    fi
}

#===============================================================================
# Main
#===============================================================================
main() {
    acquire_lock

    # Maintenance mode: lobster stop sets this flag to prevent auto-restart.
    # The flag is honored indefinitely — it is only cleared by:
    #   1. on-fresh-start.py when the dispatcher session starts successfully
    #   2. lobster start (cmd_start in src/cli) explicitly before starting services
    #
    # This makes `lobster stop` a true pause: the system stays down until an
    # explicit restart, not just until a 1-hour timer expires (issue #1656).
    if [[ -f "$MAINTENANCE_FLAG" ]]; then
        log_info "=== Maintenance mode active — skipping all checks (flag: $MAINTENANCE_FLAG) ==="
        exit 0
    fi

    log_info "=== Health check v3 starting ==="

    local level="GREEN"
    local restart_reason=""

    # --- Read lifecycle state ---
    local lobster_mode
    lobster_mode=$(read_lobster_mode)
    local state_age
    state_age=$(read_state_age)
    log_info "Lifecycle state: mode=$lobster_mode, state_age=${state_age}s"

    # --- Check for recent compaction (suppress stale-inbox false-positives) ---
    # Claude Code pauses tool calls during context compaction for 1-3+ minutes.
    # If on-compact.py recorded a compacted_at within the last
    # COMPACTION_SUPPRESS_SECONDS, skip all stale-inbox checks this run.
    # Additionally, check last-compact.ts for a 10-minute grace period that
    # covers the post-compaction re-orientation window (reading bootup files,
    # processing inbox backlog, waiting for compact-catchup to complete).
    local compaction_recent=false
    if is_compaction_recent || is_compact_grace_period; then
        compaction_recent=true
    fi

    # --- Boot grace period check ---
    # After a restart (health-check-initiated or manual), skip stale-inbox,
    # WFM freshness, and process/tmux checks for BOOT_GRACE_SECONDS. This
    # prevents false-positive alerts during the ~60-90s a fresh session needs
    # to initialize and start draining the inbox. Resource, auth, and service
    # checks still run — they are fast and restart-independent.
    local boot_grace=false
    if is_boot_grace_period; then
        boot_grace=true
    fi

    # --- Always check systemd services (includes router/bot) ---
    if ! check_services; then
        level="RED"
        restart_reason="systemd service not active"
    fi

    # --- Lifecycle-aware Claude checks ---
    #
    # The persistent wrapper (claude-persistent.sh) manages Claude's lifecycle.
    # We need to check differently depending on the current phase:
    #
    # active:     Claude should be running. Full checks apply.
    # starting/restarting/waking: Transient states. Wrapper is handling it.
    # backoff:    Wrapper hit rapid-restart limit. Expected pause.
    # stopped:    Wrapper was stopped. Systemd should restart.
    # unknown:    No state file. Either first run or old-style wrapper.
    # hibernate:  DEPRECATED (PR #1447) — dispatcher no longer writes this state.
    #             Treated as active if seen in a stale state file.
    #

    case "$lobster_mode" in
        starting|restarting|waking)
            # Boot grace: skip stale-transient escalation and inbox checks.
            if [[ "$boot_grace" == "true" ]]; then
                log_info "Transient state check and inbox drain suppressed (boot grace period)"
            else
                # Transient states — allow some time before alarming
                if is_transient_state "$lobster_mode" "$state_age"; then
                    log_info "TRANSIENT: mode=$lobster_mode for ${state_age}s — within expected window"
                    # Don't check for Claude process during transient states
                else
                    # Stale transient state is itself RED — something is stuck.
                    # Don't wait for inbox to pile up; the state being stale IS the signal.
                    log_error "STALE TRANSIENT: mode=$lobster_mode for ${state_age}s — exceeded expected window"
                    level="RED"
                    restart_reason="stale $lobster_mode state (${state_age}s)"
                fi

                # Still check inbox drain
                if [[ "$compaction_recent" == "true" ]]; then
                    log_info "Inbox drain suppressed (recent compaction)"
                else
                    check_inbox_drain
                    local transient_inbox_rc=$?
                    if [[ $transient_inbox_rc -eq 2 ]]; then
                        if is_recent_restart; then
                            [[ "$level" == "GREEN" ]] && level="YELLOW"
                        else
                            level="RED"
                            restart_reason="${restart_reason:+$restart_reason + }stale inbox (>$((STALE_THRESHOLD_SECONDS/60))m)"
                        fi
                    elif [[ $transient_inbox_rc -eq 1 && "$level" == "GREEN" ]]; then
                        level="YELLOW"
                    elif [[ $transient_inbox_rc -eq 0 ]]; then
                        clear_stale_inbox_markers
                    fi
                fi
            fi
            ;;

        backoff)
            if is_transient_state "$lobster_mode" "$state_age"; then
                log_info "BACKOFF: Wrapper cooling down (${state_age}s) — expected behavior"
            else
                log_warn "EXTENDED BACKOFF: ${state_age}s — may need intervention"
                if [[ "$level" == "GREEN" ]]; then
                    level="YELLOW"
                fi
            fi

            # Check inbox drain even during backoff
            if [[ "$boot_grace" == "true" ]]; then
                log_info "Inbox drain suppressed (boot grace period)"
            elif [[ "$compaction_recent" == "true" ]]; then
                log_info "Inbox drain suppressed (recent compaction)"
            else
                check_inbox_drain
                local backoff_inbox_rc=$?
                if [[ $backoff_inbox_rc -eq 2 ]]; then
                    if is_recent_restart; then
                        [[ "$level" == "GREEN" ]] && level="YELLOW"
                    else
                        level="RED"
                        restart_reason="${restart_reason:+$restart_reason + }stale inbox during backoff"
                    fi
                elif [[ $backoff_inbox_rc -eq 0 ]]; then
                    clear_stale_inbox_markers
                fi
            fi
            ;;

        stopped)
            # Wrapper was intentionally stopped — systemd should catch this
            log_warn "STOPPED: Wrapper received shutdown signal"
            # Let systemd handle restart; don't duplicate
            ;;

        active-debug)
            # Debug mode: claude-wrapper.exp runs Claude interactively.
            # No persistent wrapper process expected — only check tmux and Claude.
            if [[ "$boot_grace" == "true" ]]; then
                log_info "Process/inbox checks suppressed (boot grace period)"
            else
                log_info "ACTIVE-DEBUG: Debug mode active, checking tmux and Claude process"
                if ! check_tmux; then
                    level="RED"
                    restart_reason="tmux session missing (debug mode)"
                fi

                if ! check_claude_process; then
                    level="RED"
                    restart_reason="${restart_reason:+$restart_reason + }no Claude process in lobster tmux (debug mode)"
                fi

                # Inbox drain check
                if [[ "$compaction_recent" == "true" ]]; then
                    log_info "Inbox drain suppressed (recent compaction)"
                else
                    check_inbox_drain
                    local debug_inbox_rc=$?
                    if [[ $debug_inbox_rc -eq 2 ]]; then
                        if is_recent_restart; then
                            [[ "$level" == "GREEN" ]] && level="YELLOW"
                        else
                            level="RED"
                            restart_reason="${restart_reason:+$restart_reason + }stale inbox (>$((STALE_THRESHOLD_SECONDS/60))m)"
                        fi
                    elif [[ $debug_inbox_rc -eq 1 && "$level" == "GREEN" ]]; then
                        level="YELLOW"
                    elif [[ $debug_inbox_rc -eq 0 ]]; then
                        clear_stale_inbox_markers
                    fi
                fi
            fi
            ;;

        hibernate|active|unknown|*)
            # hibernate: DEPRECATED — dispatcher no longer writes this state (PR #1447).
            # If seen in a stale state file, treat as active: full process and inbox checks apply.
            if [[ "$lobster_mode" == "hibernate" ]]; then
                log_warn "HIBERNATE: stale hibernate state found — dispatcher no longer uses hibernation (treating as active)"
            fi

            # Boot grace: skip process/tmux/inbox checks — session may still be initializing.
            if [[ "$boot_grace" == "true" ]]; then
                log_info "Process/inbox checks suppressed (boot grace period)"
            else
                # Standard checks: wrapper + Claude should be running
                if ! check_tmux; then
                    level="RED"
                    restart_reason="tmux session missing"
                fi

                # In persistent mode, check for wrapper OR Claude process
                # The wrapper is always running; Claude may be temporarily absent
                # during restarts, but the wrapper handles that.
                local has_wrapper=false
                local has_claude=false

                if check_wrapper_process; then
                    has_wrapper=true
                fi
                if check_claude_process; then
                    has_claude=true
                fi

                if [[ "$has_wrapper" == "false" && "$has_claude" == "false" ]]; then
                    level="RED"
                    restart_reason="${restart_reason:+$restart_reason + }no wrapper or Claude process in lobster tmux"
                elif [[ "$has_wrapper" == "false" && "$has_claude" == "true" ]]; then
                    # Claude running without persistent wrapper.
                    # In debug mode (LOBSTER_DEBUG=true) this is expected: claude-wrapper.exp
                    # runs Claude interactively without the persistent wrapper lifecycle.
                    # Suppress the warning to avoid noise.
                    if [[ "$LOBSTER_DEBUG" == "true" ]]; then
                        log_info "Claude running without persistent wrapper (debug mode — expected)"
                    else
                        log_warn "Claude running without persistent wrapper (old-style mode?)"
                    fi
                elif [[ "$has_wrapper" == "true" && "$has_claude" == "false" ]]; then
                    # Wrapper running but no Claude — could be between launches
                    # Check state age: if it's been a while, something may be stuck
                    if [[ $state_age -gt 120 && "$lobster_mode" == "active" ]]; then
                        log_warn "Wrapper running but no Claude for ${state_age}s in active state"
                        if [[ "$level" == "GREEN" ]]; then
                            level="YELLOW"
                        fi
                    else
                        log_info "Wrapper running, Claude temporarily absent (state: $lobster_mode, age: ${state_age}s)"
                    fi
                fi

                # Inbox drain check
                if [[ "$compaction_recent" == "true" ]]; then
                    log_info "Inbox drain suppressed (recent compaction)"
                else
                    check_inbox_drain
                    local inbox_rc=$?
                    if [[ $inbox_rc -eq 2 ]]; then
                        if is_recent_restart; then
                            [[ "$level" == "GREEN" ]] && level="YELLOW"
                        else
                            level="RED"
                            restart_reason="${restart_reason:+$restart_reason + }stale inbox (>$((STALE_THRESHOLD_SECONDS/60))m)"
                        fi
                    elif [[ $inbox_rc -eq 1 && "$level" == "GREEN" ]]; then
                        level="YELLOW"
                    elif [[ $inbox_rc -eq 0 ]]; then
                        clear_stale_inbox_markers
                    fi
                fi
            fi
            ;;
    esac

    # --- Outbox drain check (are replies being delivered?) ---

    check_outbox_drain
    local outbox_rc=$?
    if [[ $outbox_rc -eq 2 ]]; then
        level="RED"
        restart_reason="${restart_reason:+$restart_reason + }stale outbox (>$((OUTBOX_STALE_THRESHOLD_SECONDS/60))m)"
    elif [[ $outbox_rc -eq 1 && "$level" == "GREEN" ]]; then
        level="YELLOW"
    fi

    # --- Dispatcher heartbeat check (issue #1483 simplification) ---
    # Single-file liveness check: hooks/thinking-heartbeat.py writes a Unix
    # epoch timestamp to DISPATCHER_HEARTBEAT_FILE on every PostToolUse event.
    # The 20-minute threshold covers compaction + catchup without suppression.
    #
    # Only suppressed during:
    #   - Hibernation (dispatcher process is not running)
    #   - Transient lifecycle states (starting/restarting/waking/backoff/stopped —
    #     the wrapper hasn't even launched Claude yet)
    # Boot grace and catchup suppression are no longer needed: the threshold
    # absorbs them. The dispatcher no longer needs to call record-catchup-state.sh.

    if is_hibernating; then
        log_info "Dispatcher heartbeat suppressed (hibernating)"
    elif [[ "$lobster_mode" == "starting" || "$lobster_mode" == "restarting" || \
            "$lobster_mode" == "waking"    || "$lobster_mode" == "backoff"    || \
            "$lobster_mode" == "stopped" ]]; then
        log_info "Dispatcher heartbeat suppressed (transient lifecycle state: $lobster_mode)"
    else
        check_dispatcher_heartbeat
        local hb_rc=$?
        if [[ $hb_rc -eq 2 ]]; then
            level="RED"
            restart_reason="${restart_reason:+$restart_reason + }dispatcher heartbeat stale (>${DISPATCHER_HEARTBEAT_STALE_SECONDS}s)"
        fi
    fi

    # --- Auth token check (proactive expiry warning) ---

    check_auth_token
    local auth_rc=$?
    if [[ $auth_rc -eq 2 ]]; then
        # Auth confirmed RED (loggedIn=false after consecutive checks).
        # IMPORTANT: Do NOT pass this to do_restart(). Restarting Claude
        # cannot fix an auth problem and causes a crash loop. Instead:
        # - Alert via Telegram so the operator knows manual action is needed
        # - Keep level at YELLOW (not RED) so do_restart() is never invoked
        #   for auth failures alone
        send_telegram_alert_deduped "auth-expired" "Lobster: Claude auth expired (loggedIn=false confirmed after consecutive checks).

Restarting will NOT fix this. Manual action required:
ssh into the server and run: claude auth login"
        if [[ "$level" != "RED" ]]; then
            level="YELLOW"
        fi
    elif [[ $auth_rc -eq 1 && "$level" == "GREEN" ]]; then
        level="YELLOW"
    fi

    # --- Dashboard server check (soft restart, never RED) ---

    check_dashboard_server

    # --- Cron entry guard (auto-restore SELF-CHECK if missing) ---

    check_cron_entries

    # --- messages.db liveness (advisory, BIS-167 Slice 6, never RED) ---

    check_messages_db

    # --- Resource checks (RED if critical) ---

    if ! check_memory; then
        level="RED"
        restart_reason="${restart_reason:+$restart_reason + }memory critical"
    fi

    if ! check_disk; then
        # Disk full is not fixable by restart, just alert
        if [[ "$level" != "RED" ]]; then
            level="YELLOW"
        fi
        log_warn "Disk space low - restart won't help, needs manual cleanup"
    fi

    # --- Act on level ---

    case "$level" in
        GREEN)
            log_info "GREEN: All checks passed (mode=$lobster_mode)"
            # If system previously required manual intervention, clear the flag now
            # so auto-restarts are re-enabled after genuine recovery.
            clear_manual_intervention
            ;;
        YELLOW)
            log_warn "YELLOW: Non-critical issues detected (mode=$lobster_mode), monitoring"
            ;;
        RED)
            log_error "RED: Critical failure (mode=$lobster_mode) - $restart_reason"
            # Pass compaction_recent as suppress_alert so do_restart() skips its
            # Telegram alerts when compaction is still within the suppression
            # window.  on-compact.py already sent exactly one notification, so
            # the deferred-restart and recovered-successfully alerts would be
            # duplicates.  Genuine failure alerts (restart failed, BLACK state)
            # always fire regardless of this flag.
            do_restart "$restart_reason" "$compaction_recent"
            ;;
    esac

    log_info "=== Health check v3 complete (level=$level, mode=$lobster_mode) ==="
}

# Handle --clear-black before entering the main loop.
# This is a maintenance escape hatch: it resets the BLACK state so auto-restarts
# are re-enabled without requiring a full manual recovery procedure.
if [[ "${1:-}" == "--clear-black" ]]; then
    echo "0 0 GREEN" > "$RESTART_STATE_FILE"
    echo "BLACK state cleared — restart state reset to GREEN"
    exit 0
fi

main "$@"
