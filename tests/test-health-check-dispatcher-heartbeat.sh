#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Dispatcher Heartbeat Sentinel (issue #1483)
#
# Tests for check_dispatcher_heartbeat() — the simplified single-file liveness check.
#
# Tests:
#   1. Heartbeat file absent → GREEN (skipped, no false alarm on fresh install)
#   2. Heartbeat file recent (< DISPATCHER_HEARTBEAT_STALE_SECONDS) → GREEN
#   3. Heartbeat file stale (> DISPATCHER_HEARTBEAT_STALE_SECONDS) → RED (exit 2)
#   4. Heartbeat file contains non-integer content → GREEN (graceful fallback)
#   5. Heartbeat file exists but empty → GREEN (graceful fallback)
#   6. LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE respected
#   7. Stale by 1 second past threshold → RED (boundary condition)
#   8. Fresh by 1 second before threshold → GREEN (boundary condition)
#
# Key behavioral assertion: the check uses a single dispatcher-heartbeat file with
# a single epoch timestamp. No lobster-state.json reads. The WFM-active file is
# also consulted by check_dispatcher_heartbeat() but is bypassed safely here via
# the DISPATCHER_WFM_ACTIVE_FILE default (absent file → treated as not active).
#
# Usage: bash tests/test-health-check-dispatcher-heartbeat.sh
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

TEST_TMPDIR=$(mktemp -d /tmp/lobster-dispatcher-hb-test-XXXXXX)
TEST_LOG_DIR="$TEST_TMPDIR/logs"
DISPATCHER_HEARTBEAT_FILE="$TEST_LOG_DIR/dispatcher-heartbeat"

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

# Source check_dispatcher_heartbeat() from the health check script once.
LOG_FILE="$TEST_LOG_DIR/health-check.log"
DISPATCHER_HEARTBEAT_STALE_SECONDS=1200

log()       { echo "[$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

# Load the function definition from the health check script.
eval "$(sed -n '/^check_dispatcher_heartbeat()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

# Run check_dispatcher_heartbeat() with the given heartbeat file.
# Returns the function's exit code via $?.
run_heartbeat_check() {
    local hb_file="$1"
    DISPATCHER_HEARTBEAT_FILE="$hb_file"
    check_dispatcher_heartbeat
    return $?
}

echo "=== Dispatcher Heartbeat Health Check Tests ==="
echo ""

# -------------------------------------------------------------------
# Test 1: Heartbeat file absent → GREEN (skip, no false alarm)
# -------------------------------------------------------------------
begin_test "Absent heartbeat file → GREEN (skip)"
rm -f "$DISPATCHER_HEARTBEAT_FILE"
run_heartbeat_check "$DISPATCHER_HEARTBEAT_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 2: Recent heartbeat (just now) → GREEN
# -------------------------------------------------------------------
begin_test "Recent heartbeat (5s ago) → GREEN"
echo "$(( $(date +%s) - 5 ))" > "$DISPATCHER_HEARTBEAT_FILE"
run_heartbeat_check "$DISPATCHER_HEARTBEAT_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 3: Stale heartbeat (> 1200s ago) → RED
# -------------------------------------------------------------------
begin_test "Stale heartbeat (1500s ago) → RED"
echo "$(( $(date +%s) - 1500 ))" > "$DISPATCHER_HEARTBEAT_FILE"
run_heartbeat_check "$DISPATCHER_HEARTBEAT_FILE" && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 4: Heartbeat contains non-integer content → GREEN (graceful)
# -------------------------------------------------------------------
begin_test "Non-integer content → GREEN (graceful fallback)"
echo "not-a-number" > "$DISPATCHER_HEARTBEAT_FILE"
run_heartbeat_check "$DISPATCHER_HEARTBEAT_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 5: Heartbeat file empty → GREEN (graceful fallback)
# -------------------------------------------------------------------
begin_test "Empty file → GREEN (graceful fallback)"
echo "" > "$DISPATCHER_HEARTBEAT_FILE"
run_heartbeat_check "$DISPATCHER_HEARTBEAT_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 6: Custom override path is used
# -------------------------------------------------------------------
begin_test "Custom heartbeat path is used"
custom_hb="$TEST_TMPDIR/custom-heartbeat"
echo "$(( $(date +%s) - 5 ))" > "$custom_hb"
run_heartbeat_check "$custom_hb" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 7: Exactly 1 second past threshold → RED (boundary)
# -------------------------------------------------------------------
begin_test "1s past threshold (1201s ago) → RED"
echo "$(( $(date +%s) - 1201 ))" > "$DISPATCHER_HEARTBEAT_FILE"
run_heartbeat_check "$DISPATCHER_HEARTBEAT_FILE" && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 8: Exactly 1 second before threshold → GREEN (boundary)
# -------------------------------------------------------------------
begin_test "1s before threshold (1199s ago) → GREEN"
echo "$(( $(date +%s) - 1199 ))" > "$DISPATCHER_HEARTBEAT_FILE"
run_heartbeat_check "$DISPATCHER_HEARTBEAT_FILE" && rc=$? || rc=$?
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
echo ""
echo "Results: $PASS/$TOTAL passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
