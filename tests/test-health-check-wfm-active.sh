#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - WFM-Active Signal (issue #949)
#
# Tests for the WFM-active bypass in check_dispatcher_heartbeat():
# When the dispatcher is blocked inside wait_for_messages, PostToolUse hooks
# do not fire and the dispatcher-heartbeat file goes stale. A separate
# dispatcher-wfm-active file (written and periodically refreshed by
# inbox_server.py during the wait loop) lets the health check distinguish
# "dispatcher idle in WFM" from "dispatcher frozen/dead".
#
# Tests:
#   1. Stale heartbeat + absent WFM-active → RED (baseline, no change)
#   2. Stale heartbeat + fresh WFM-active → GREEN (dispatcher is alive in WFM)
#   3. Stale heartbeat + stale WFM-active → RED (WFM itself looks frozen)
#   4. Stale heartbeat + non-integer WFM-active → RED (graceful: treat as stale)
#   5. Stale heartbeat + empty WFM-active → RED (graceful: treat as stale)
#   6. Fresh heartbeat + fresh WFM-active → GREEN (both fresh, no regression)
#   7. DISPATCHER_WFM_ACTIVE_FILE can be set to a custom path
#   8. WFM-active staleness threshold boundary: 1s before → GREEN
#   9. WFM-active staleness threshold boundary: 1s after → RED
#
# Usage: bash tests/test-health-check-wfm-active.sh
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

log()       { echo "[$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

# Load the function definition from the health check script.
eval "$(sed -n '/^check_dispatcher_heartbeat()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

# Also load the WFM_ACTIVE_STALE_SECONDS constant.
WFM_ACTIVE_STALE_SECONDS=$(grep '^WFM_ACTIVE_STALE_SECONDS=' "$HEALTH_SCRIPT" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
if [[ -z "$WFM_ACTIVE_STALE_SECONDS" ]]; then
    echo "ERROR: WFM_ACTIVE_STALE_SECONDS not found in $HEALTH_SCRIPT"
    exit 1
fi

echo "=== WFM-Active Signal Health Check Tests ==="
echo "DISPATCHER_HEARTBEAT_STALE_SECONDS=$DISPATCHER_HEARTBEAT_STALE_SECONDS"
echo "WFM_ACTIVE_STALE_SECONDS=$WFM_ACTIVE_STALE_SECONDS"
echo ""

DISPATCHER_HB_FILE="$TEST_LOG_DIR/dispatcher-heartbeat"
WFM_ACTIVE_FILE="$TEST_LOG_DIR/dispatcher-wfm-active"
STALE_HEARTBEAT=$(( $(date +%s) - 1500 ))   # 1500s ago — past 1200s threshold
RECENT_HEARTBEAT=$(( $(date +%s) - 5 ))      # 5s ago — well within threshold

run_check() {
    local hb_file="$1"
    local wfm_file="$2"
    DISPATCHER_HEARTBEAT_FILE="$hb_file"
    DISPATCHER_WFM_ACTIVE_FILE="$wfm_file"
    check_dispatcher_heartbeat
    return $?
}

# -------------------------------------------------------------------
# Test 1: Stale heartbeat + absent WFM-active → RED (baseline)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + absent WFM-active → RED"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
rm -f "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 2: Stale heartbeat + fresh WFM-active → GREEN (alive in WFM)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + fresh WFM-active → GREEN"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "$(( $(date +%s) - 30 ))" > "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 3: Stale heartbeat + stale WFM-active → RED (WFM frozen)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + stale WFM-active → RED"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "$(( $(date +%s) - WFM_ACTIVE_STALE_SECONDS - 60 ))" > "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 4: Stale heartbeat + non-integer WFM-active → RED (graceful)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + non-integer WFM-active → RED"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "not-a-number" > "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 5: Stale heartbeat + empty WFM-active → RED (graceful)
# -------------------------------------------------------------------
begin_test "Stale heartbeat + empty WFM-active → RED"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "" > "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 6: Fresh heartbeat + fresh WFM-active → GREEN (no regression)
# -------------------------------------------------------------------
begin_test "Fresh heartbeat + fresh WFM-active → GREEN"
echo "$RECENT_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "$(( $(date +%s) - 30 ))" > "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 7: DISPATCHER_WFM_ACTIVE_FILE can be set to a custom path
# -------------------------------------------------------------------
begin_test "Custom DISPATCHER_WFM_ACTIVE_FILE path is used"
custom_wfm="$TEST_TMPDIR/custom-wfm-active"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "$(( $(date +%s) - 30 ))" > "$custom_wfm"
DISPATCHER_WFM_ACTIVE_FILE="$custom_wfm"
DISPATCHER_HEARTBEAT_FILE="$DISPATCHER_HB_FILE"
check_dispatcher_heartbeat && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 8: WFM-active 1s before threshold → GREEN (boundary)
# -------------------------------------------------------------------
begin_test "WFM-active 1s before threshold → GREEN"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "$(( $(date +%s) - WFM_ACTIVE_STALE_SECONDS + 1 ))" > "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 9: WFM-active 1s past threshold → RED (boundary)
# -------------------------------------------------------------------
begin_test "WFM-active 1s past threshold → RED"
echo "$STALE_HEARTBEAT" > "$DISPATCHER_HB_FILE"
echo "$(( $(date +%s) - WFM_ACTIVE_STALE_SECONDS - 1 ))" > "$WFM_ACTIVE_FILE"
run_check "$DISPATCHER_HB_FILE" "$WFM_ACTIVE_FILE" && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
echo ""
echo "Results: $PASS/$TOTAL passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
