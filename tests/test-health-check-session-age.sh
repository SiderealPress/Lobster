#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Session Age Check (issue #2059)
#
# Tests for check_session_age() — the proactive restart at
# SESSION_AGE_LIMIT_SECONDS (7200s), an operational precaution against
# sessions dying uncleanly (no Stop hook, no tombstone) around that age.
#
# NOTE: issue #2059's original justification — that Claude Code enforces a
# hard 7440s session lifetime — was based on only 2 observed data points and
# was never confirmed against Anthropic documentation. See issue #2157 for
# the reassessment. check_session_age() triggers a graceful SIGTERM at
# SESSION_AGE_LIMIT_SECONDS (7200s) regardless of whether that original
# claim holds — the precaution has value on its own.
#
# Tests:
#   1. No start timestamp file → returns 0 (GREEN, no action)
#   2. Young session (age < SESSION_AGE_LIMIT_SECONDS) → returns 0 (GREEN)
#   3. Session at exact limit → sends SIGTERM, returns 1
#   4. Session past limit (age > SESSION_AGE_LIMIT_SECONDS) → sends SIGTERM, returns 1
#   5. SIGTERM sent to live dispatcher PID
#   6. No dispatcher.pid file → returns 0 (cannot act, skip gracefully)
#   7. Malformed start timestamp (non-integer) → returns 0 (graceful fallback)
#   8. Empty start timestamp file → returns 0 (graceful fallback)
#   9. Start file deleted after SIGTERM (prevents double-fire on next health check)
#  10. Boot grace suppression: caller suppresses check during boot grace period
#      (check_session_age itself does not implement boot grace — suppression is in main())
#
# Usage: bash tests/test-health-check-session-age.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
HEALTH_SCRIPT="$SCRIPT_DIR/health-check-v3.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-session-age-test-XXXXXX)
TEST_LOG_DIR="$TEST_TMPDIR/logs"
TEST_DATA_DIR="$TEST_TMPDIR/data"
TEST_MESSAGES_DIR="$TEST_TMPDIR/messages"
TEST_CONFIG_DIR="$TEST_MESSAGES_DIR/config"

cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

mkdir -p "$TEST_LOG_DIR" "$TEST_DATA_DIR" "$TEST_CONFIG_DIR" "$TEST_TMPDIR/alert-dedup"

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_exit() {
    local actual="$1" expected="$2"
    if [[ "$actual" -eq "$expected" ]]; then pass; else fail "expected exit $expected, got $actual"; fi
}

#===============================================================================
# Named constants (from the spec — never hardcode magic values in tests)
#===============================================================================
SESSION_AGE_LIMIT_SECONDS=7200        # from health-check-v3.sh default
DISPATCHER_SESSION_START_FILENAME="dispatcher-session-start.ts"
DISPATCHER_PID_FILENAME="dispatcher.pid"

#===============================================================================
# Stub the minimal environment for check_session_age() to run
#===============================================================================

LOG_FILE="$TEST_LOG_DIR/health-check.log"

log()       { echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

# Stub send_telegram_alert_deduped: write to a file so we can assert it was called.
TELEGRAM_ALERTS_FILE="$TEST_TMPDIR/telegram-alerts.txt"
send_telegram_alert_deduped() {
    echo "ALERT[$1]: $2" >> "$TELEGRAM_ALERTS_FILE"
}

# Override env vars that check_session_age() reads.
DISPATCHER_SESSION_START_FILE="$TEST_DATA_DIR/$DISPATCHER_SESSION_START_FILENAME"
DISPATCHER_PID_FILE="$TEST_CONFIG_DIR/$DISPATCHER_PID_FILENAME"
ALERT_DEDUP_DIR="$TEST_TMPDIR/alert-dedup"

# Load check_session_age() from the health check script.
# We extract just the function to avoid sourcing the entire ~2000-line file.
eval "$(sed -n '/^check_session_age()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

if ! declare -f check_session_age > /dev/null 2>&1; then
    echo "FATAL: check_session_age() not found in $HEALTH_SCRIPT"
    exit 1
fi

#===============================================================================
# Helper: reset test state between tests
#===============================================================================
reset_state() {
    rm -f "$DISPATCHER_SESSION_START_FILE" "$DISPATCHER_PID_FILE"
    rm -f "$TELEGRAM_ALERTS_FILE"
}

#===============================================================================
# Tests
#===============================================================================

echo ""
echo "=== Health Check Session Age Tests ==="
echo ""

# 1. No start timestamp file → returns 0 (GREEN, no action)
begin_test "no_start_file_returns_green"
reset_state
check_session_age
assert_exit $? 0

# 2. Young session (age = 0s) → returns 0 (GREEN)
begin_test "young_session_returns_green"
reset_state
echo "$(date +%s)" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 3. Session just under the limit (SESSION_AGE_LIMIT_SECONDS - 1) → returns 0
begin_test "session_just_under_limit_returns_green"
reset_state
early_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS + 1 ))
echo "$early_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 4. Session at exact limit → sends SIGTERM, returns 1
# We use a dummy process to receive SIGTERM.
begin_test "session_at_exact_limit_sends_sigterm"
reset_state
# Start a background sleep process to receive the SIGTERM.
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
at_limit_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS ))
echo "$at_limit_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
kill "$target_pid" 2>/dev/null || true  # clean up if SIGTERM didn't kill it
assert_exit $rc 1

# 5. Session past limit → sends SIGTERM, returns 1
begin_test "session_past_limit_sends_sigterm"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_limit_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_limit_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
kill "$target_pid" 2>/dev/null || true
assert_exit $rc 1

# 6. No dispatcher.pid file → returns 0 (cannot send SIGTERM, skips gracefully)
begin_test "no_pid_file_returns_green"
reset_state
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
# No DISPATCHER_PID_FILE written
check_session_age
assert_exit $? 0

# 7. Malformed start timestamp (non-integer) → returns 0 (graceful fallback)
begin_test "malformed_start_timestamp_returns_green"
reset_state
echo "not-a-number" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 8. Empty start timestamp file → returns 0 (graceful fallback)
begin_test "empty_start_file_returns_green"
reset_state
# shellcheck disable=SC2188
> "$DISPATCHER_SESSION_START_FILE"  # empty file
check_session_age
assert_exit $? 0

# 9. Start file deleted after SIGTERM (prevents double-fire on next health check run)
begin_test "start_file_deleted_after_sigterm"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_limit_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_limit_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ ! -f "$DISPATCHER_SESSION_START_FILE" ]]; then
    pass
else
    fail "start file still present after SIGTERM — double-fire risk on next health check"
fi

# 10. Dead PID in dispatcher.pid → returns 0 (cannot send SIGTERM to dead process)
begin_test "dead_pid_returns_green"
reset_state
# Find a PID that is definitely not alive.
dead_pid=999997
while kill -0 "$dead_pid" 2>/dev/null; do
    dead_pid=$((dead_pid - 1))
done
echo "$dead_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 11. Telegram alert is sent when SIGTERM fires (LOBSTER_DEBUG=true)
begin_test "telegram_alert_sent_on_sigterm"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
LOBSTER_DEBUG=true check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ -f "$TELEGRAM_ALERTS_FILE" ]] && grep -q "proactive-session-restart" "$TELEGRAM_ALERTS_FILE"; then
    pass
else
    fail "no Telegram alert with key 'proactive-session-restart' found after SIGTERM"
fi

# 12. Notification text is user-friendly (issue #2075): plain language, not raw
# PID / session-age implementation detail ("Dispatcher PID N sent SIGTERM" /
# "before the 7440s CC hard limit").
begin_test "notification_text_is_user_friendly"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
LOBSTER_DEBUG=true check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ -f "$TELEGRAM_ALERTS_FILE" ]]; then
    alert_text=$(cat "$TELEGRAM_ALERTS_FILE")
    if echo "$alert_text" | grep -qiE "planned|graceful restart|back in" \
        && ! echo "$alert_text" | grep -qE "Dispatcher PID|7440s"; then
        pass
    else
        fail "notification text is not plain-language (or still leaks implementation detail): $alert_text"
    fi
else
    fail "no Telegram alert file found"
fi

# 13. Notification fires BEFORE SIGTERM (issue #2075): verify the source ordering
# structurally. bash is single-threaded within a function body; if
# send_telegram_alert_deduped appears before the executable 'kill -TERM' line in
# check_session_age(), the alert is guaranteed to fire before SIGTERM delivery —
# so a session that dies immediately on SIGTERM still got the message out.
begin_test "notification_fires_before_sigterm"
fn_body=$(sed -n '/^check_session_age()/,/^}/p' "$HEALTH_SCRIPT")
alert_line=$(echo "$fn_body" | grep -n "send_telegram_alert_deduped" | head -1 | cut -d: -f1)
kill_line=$(echo "$fn_body" | grep -n 'if kill -TERM' | head -1 | cut -d: -f1)
if [[ -z "$alert_line" || -z "$kill_line" ]]; then
    fail "could not locate send_telegram_alert_deduped or executable 'kill -TERM' in check_session_age() — alert_line='$alert_line' kill_line='$kill_line'"
elif [[ "$alert_line" -lt "$kill_line" ]]; then
    pass
else
    fail "send_telegram_alert_deduped (line $alert_line) is NOT before kill -TERM (line $kill_line) in check_session_age()"
fi

# 14. LOBSTER_DEBUG not set (or false) → alert still suppressed entirely (existing
# noise-reduction behavior from 2026-06-14 must be preserved by this fix — the
# ordering/wording fix must not turn this back on for non-debug instances).
begin_test "telegram_alert_suppressed_when_debug_false"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
LOBSTER_DEBUG=false check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ ! -f "$TELEGRAM_ALERTS_FILE" ]]; then
    pass
else
    fail "Telegram alert was sent even though LOBSTER_DEBUG=false: $(cat "$TELEGRAM_ALERTS_FILE")"
fi

#===============================================================================
# Summary
#===============================================================================
echo ""
echo "Results: $PASS passed, $FAIL failed, $TOTAL total"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All session age tests passed.${NC}"
    exit 0
else
    echo -e "${RED}$FAIL test(s) failed.${NC}"
    exit 1
fi
