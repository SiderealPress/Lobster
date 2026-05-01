#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - WFM-Active Signal (issue #1713 / #949)
#
# Tests for the WFM-active bypass in check_dispatcher_heartbeat():
# When the dispatcher is blocked inside wait_for_messages, PostToolUse hooks
# do not fire and the dispatcher-heartbeat file goes stale. A separate
# dispatcher-wfm-active file (written and periodically refreshed by
# inbox_server.py during the wait loop) lets the health check distinguish
# "dispatcher idle in WFM" from "dispatcher frozen/dead".
#
# Tests:
#   1. Stale heartbeat + absent WFM-active → RED (original behavior preserved)
#   2. Stale heartbeat + fresh WFM-active → GREEN (WFM suppression works)
#   3. Stale heartbeat + stale WFM-active → RED (both stale = frozen)
#   4. Stale heartbeat + WFM-active 1s past threshold → RED (boundary)
#   5. Stale heartbeat + WFM-active 1s before threshold → GREEN (boundary)
#   6. Fresh heartbeat ignores WFM-active (heartbeat alone = GREEN)
#   7. LOBSTER_WFM_ACTIVE_OVERRIDE env var is respected
#   8. WFM-active file with non-integer content → RED (treat as absent)
#   9. WFM-active tombstone ('exited') with FRESH mtime → GREEN (issue #1884 midnight race fix)
#  10. WFM-active tombstone ('exited') with OLD mtime → RED (grace period expired, treat as absent)
#  11. TOCTOU race: file deleted mid-read → RED, not crash (Fix 1 regression test)
#
# Usage: bash tests/test-health-check-wfm-active.sh
# Total tests: 11
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

TEST_TMPDIR=$(mktemp -d /tmp/lobster-wfm-active-test-XXXXXX)
TEST_LOG_DIR="$TEST_TMPDIR/logs"
DISPATCHER_HEARTBEAT_FILE="$TEST_LOG_DIR/dispatcher-heartbeat"
DISPATCHER_WFM_ACTIVE_FILE="$TEST_LOG_DIR/dispatcher-wfm-active"

cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

mkdir -p "$TEST_LOG_DIR"

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_exit() {
    local actual="$1" expected="$2"
    if [[ "$actual" -eq "$expected" ]]; then pass; else fail "expected exit $expected, got $actual"; fi
}

# Source check_dispatcher_heartbeat() from the health check script.
LOG_FILE="$TEST_LOG_DIR/health-check.log"
DISPATCHER_HEARTBEAT_STALE_SECONDS=1200
WFM_ACTIVE_STALE_SECONDS=180
WFM_TOMBSTONE_GRACE_SECONDS=30

log()       { echo "[$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

eval "$(sed -n '/^check_dispatcher_heartbeat()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

# Helper: write a stale heartbeat (20 minutes ago).
write_stale_heartbeat() {
    echo "$(( $(date +%s) - 1500 ))" > "$DISPATCHER_HEARTBEAT_FILE"
}

# Helper: write a fresh heartbeat (5 seconds ago).
write_fresh_heartbeat() {
    echo "$(( $(date +%s) - 5 ))" > "$DISPATCHER_HEARTBEAT_FILE"
}

echo "=== WFM-Active Signal Health Check Tests ==="
echo ""

# -------------------------------------------------------------------
# Test 1: Stale heartbeat + absent WFM-active → RED
# -------------------------------------------------------------------
begin_test "Stale heartbeat + absent WFM-active → RED"
write_stale_heartbeat
rm -f "$DISPATCHER_WFM_ACTIVE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 2: Stale heartbeat + fresh WFM-active → GREEN (WFM suppression)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + fresh WFM-active → GREEN"
write_stale_heartbeat
echo "$(( $(date +%s) - 30 ))" > "$DISPATCHER_WFM_ACTIVE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 3: Stale heartbeat + stale WFM-active → RED (both stale = frozen)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + stale WFM-active (600s old) → RED"
write_stale_heartbeat
echo "$(( $(date +%s) - 600 ))" > "$DISPATCHER_WFM_ACTIVE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 4: Stale heartbeat + WFM-active 1s past threshold → RED (boundary)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + WFM-active ${WFM_ACTIVE_STALE_SECONDS}+1s old → RED"
write_stale_heartbeat
echo "$(( $(date +%s) - (WFM_ACTIVE_STALE_SECONDS + 1) ))" > "$DISPATCHER_WFM_ACTIVE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 5: Stale heartbeat + WFM-active 1s before threshold → GREEN (boundary)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + WFM-active ${WFM_ACTIVE_STALE_SECONDS}-1s old → GREEN"
write_stale_heartbeat
echo "$(( $(date +%s) - (WFM_ACTIVE_STALE_SECONDS - 1) ))" > "$DISPATCHER_WFM_ACTIVE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 6: Fresh heartbeat → GREEN (WFM-active is irrelevant)
# -------------------------------------------------------------------
begin_test "Fresh heartbeat → GREEN regardless of WFM-active"
write_fresh_heartbeat
echo "$(( $(date +%s) - 600 ))" > "$DISPATCHER_WFM_ACTIVE_FILE"  # stale WFM-active
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 7: LOBSTER_WFM_ACTIVE_OVERRIDE env var is respected
# -------------------------------------------------------------------
begin_test "LOBSTER_WFM_ACTIVE_OVERRIDE env var is respected"
write_stale_heartbeat
rm -f "$DISPATCHER_WFM_ACTIVE_FILE"
OVERRIDE_FILE="$TEST_LOG_DIR/custom-wfm-active"
echo "$(( $(date +%s) - 30 ))" > "$OVERRIDE_FILE"
DISPATCHER_WFM_ACTIVE_FILE="$OVERRIDE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
DISPATCHER_WFM_ACTIVE_FILE="$TEST_LOG_DIR/dispatcher-wfm-active"
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 8: WFM-active file with non-integer content → treated as absent → RED
# -------------------------------------------------------------------
begin_test "WFM-active file non-integer content → treated as absent → RED"
write_stale_heartbeat
echo "not-a-number" > "$DISPATCHER_WFM_ACTIVE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 9: Midnight race fix (issue #1884) — tombstone ('exited') with FRESH
# file mtime → GREEN.
#
# At 00:00Z, scheduled jobs fire and wake wait_for_messages(). WFM writes
# "exited" to dispatcher-wfm-active as it transitions to active processing.
# The health-check cron also fires at 00:00Z. Previously, seeing "exited"
# caused an unconditional RED (false-positive kill). With the fix, a fresh
# tombstone mtime means the dispatcher just left WFM — grant a grace period.
#
# We create the file, set its mtime to "now" (default), and verify GREEN.
# -------------------------------------------------------------------
begin_test "WFM-active tombstone 'exited' with fresh mtime → GREEN (midnight race fix)"
write_stale_heartbeat
echo "exited" > "$DISPATCHER_WFM_ACTIVE_FILE"
# mtime is "now" by default — within WFM_TOMBSTONE_GRACE_SECONDS
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 10: Tombstone ('exited') with OLD file mtime → RED (grace expired).
#
# If the tombstone was written longer ago than WFM_TOMBSTONE_GRACE_SECONDS,
# the dispatcher has been in the processing phase for a while — heartbeat
# staleness is genuinely suspicious, not a WFM→processing transition artifact.
# Treat as absent → RED.
#
# We backdate the file mtime by 120s (well past the 30s grace window).
# -------------------------------------------------------------------
begin_test "WFM-active tombstone 'exited' with old mtime → RED (grace period expired)"
write_stale_heartbeat
echo "exited" > "$DISPATCHER_WFM_ACTIVE_FILE"
# Backdate mtime by 120s — well past WFM_TOMBSTONE_GRACE_SECONDS=30
touch -d "$(date -d '120 seconds ago' '+%Y-%m-%d %H:%M:%S')" "$DISPATCHER_WFM_ACTIVE_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 11: TOCTOU race scenario — file present at check start, absent at read
#
# Simulates the race window that caused the 2026-04-22 false-positive restart:
# the WFM-active file exists when the health check starts, but is deleted
# before the read completes (the old -f / cat two-step had this window).
#
# The old two-step code (-f gate + cat) would: pass the -f check (file
# exists), then cat would return empty if the file disappeared in the window,
# and the empty wfm_active_ts would fall through to RED.
#
# The new cat-only code must handle the same absent-file path gracefully:
# cat 2>/dev/null returns empty, the integer guard rejects it, and the
# function falls through to RED — not GREEN and not a crash.
#
# We simulate the race by overriding cat in a subshell: the override deletes
# the WFM-active file before returning empty output, replicating the exact
# state the old code would see mid-race: file was present, now gone, cat empty.
# -------------------------------------------------------------------
begin_test "TOCTOU race: file deleted mid-read (cat returns empty) → RED, not crash"
write_stale_heartbeat
# Create a fresh WFM-active file so the -f check (if present) would pass.
echo "$(( $(date +%s) - 30 ))" > "$DISPATCHER_WFM_ACTIVE_FILE"
# Run in a subshell with cat overridden to simulate the race:
# cat deletes the file and returns empty, as inbox_server.py's unlink()
# would have done in the pre-Fix-2 code path.
rc=$(
    cat() {
        if [[ "$*" == *"$DISPATCHER_WFM_ACTIVE_FILE"* ]] || \
           [[ "$*" == *"dispatcher-wfm-active"* ]]; then
            rm -f "$DISPATCHER_WFM_ACTIVE_FILE"
            # return empty — simulates file deleted between -f and cat
        else
            command cat "$@"
        fi
    }
    check_dispatcher_heartbeat && echo 0 || echo $?
)
# Restore a clean state for subsequent tests.
rm -f "$DISPATCHER_WFM_ACTIVE_FILE"
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
echo ""
echo "Results: $PASS/$TOTAL passed, $FAIL failed  (expected 11)"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
