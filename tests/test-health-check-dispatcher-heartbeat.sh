#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Dispatcher Heartbeat Sentinel (issues #1483, #2074)
#
# Tests for check_dispatcher_heartbeat() — the simplified single-file liveness check.
#
# Basic heartbeat tests (issue #1483):
#   1. Heartbeat file absent → GREEN (skipped, no false alarm on fresh install)
#   2. Heartbeat file recent (< DISPATCHER_HEARTBEAT_STALE_SECONDS) → GREEN
#   3. Heartbeat file stale (> DISPATCHER_HEARTBEAT_STALE_SECONDS) → RED (exit 2)
#   4. Heartbeat file contains non-integer content → GREEN (graceful fallback)
#   5. Heartbeat file exists but empty → GREEN (graceful fallback)
#   6. LOBSTER_DISPATCHER_HEARTBEAT_OVERRIDE respected
#   7. Stale by 1 second past threshold → RED (boundary condition)
#   8. Fresh by 1 second before threshold → GREEN (boundary condition)
#
# WFM-active suppression tests (issue #2074):
#   9.  Stale heartbeat + WFM-active fresh + heartbeat age inside cap → GREEN (suppressed)
#   10. Stale heartbeat + WFM-active fresh + heartbeat age AT cap → RED (cap expired, not suppressed)
#   11. Stale heartbeat + WFM-active fresh + heartbeat age beyond cap → RED (frozen dispatcher suspected)
#   12. Stale heartbeat + WFM-active stale → RED (daemon thread also stale)
#   13. Stale heartbeat + WFM-active absent → RED (no suppression)
#   14. Stale heartbeat + WFM-active tombstone ("exited") → RED (WFM returned normally)
#   15. Stale heartbeat + WFM-active fresh + heartbeat well inside cap → GREEN
#
# Key behavioral assertion (issue #2074): WFM-active suppression is time-bounded.
# A frozen dispatcher's WFM daemon thread continues refreshing the WFM-active file
# every 60s independently of the main asyncio loop. File freshness alone does NOT
# prove the dispatcher is responsive. Once the heartbeat has been stale for
# WFM_SUPPRESSION_MAX_SECONDS, RED fires regardless of WFM-active freshness.
# Tests 9-15 exercise the *mechanism* directly with an injected cap value, so
# they remain valid regardless of what the derived default happens to be.
#
# Derivation tests (issue #2074, second incident, 2026-08-03):
#   16. compute_wfm_suppression_max_seconds(): normal case derives limit - margin
#   17. compute_wfm_suppression_max_seconds(): session-age check disabled (0) → fallback
#   18. compute_wfm_suppression_max_seconds(): margin >= limit clamps to 1, never <= 0
#   19. Regression — a heartbeat stale for 3000s (RED under the old fixed 2700s
#       cap) with a fresh WFM-active file is GREEN under the derived default
#       (SESSION_AGE_LIMIT_SECONDS=7200, margin=300 => cap=6900). This is the
#       exact false-positive that caused hourly restarts on 2026-08-03: a
#       perfectly healthy dispatcher idling in wait_for_messages for ~50 minutes.
#   20. A heartbeat stale beyond the derived cap (6900s) with WFM-active fresh
#       is still RED — the cap still catches a truly frozen dispatcher, it is
#       just no longer tighter than the session lifetime a legitimate idle wait
#       is allowed to reach.
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

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" -eq "$expected" ]]; then pass; else fail "expected $expected, got $actual"; fi
}

# Source check_dispatcher_heartbeat() from the health check script once.
LOG_FILE="$TEST_LOG_DIR/health-check.log"
DISPATCHER_HEARTBEAT_STALE_SECONDS=1200
# WFM-active variables (issues #1713, #2074): must match the values in health-check-v3.sh.
# Default to an absent file so existing basic heartbeat tests (1-8) are unaffected.
# Tests 9-15 override WFM_ACTIVE_FILE_FOR_TEST to exercise the suppression logic.
DISPATCHER_WFM_ACTIVE_FILE="$TEST_LOG_DIR/dispatcher-wfm-active-ABSENT"
WFM_ACTIVE_STALE_SECONDS=180
WFM_SUPPRESSION_MAX_SECONDS=2700

log()       { echo "[$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

# Load the function definitions from the health check script.
eval "$(sed -n '/^check_dispatcher_heartbeat()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
eval "$(sed -n '/^compute_wfm_suppression_max_seconds()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

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

# ===================================================================
# WFM-active suppression + time-cap tests (issue #2074)
#
# These tests verify that the time-bounded suppression correctly handles
# the frozen-dispatcher false-negative from the May 2026 outage.
#
# Setup: use a separate WFM-active file; override DISPATCHER_WFM_ACTIVE_FILE.
# ===================================================================

echo ""
echo "--- WFM-active suppression + time-cap tests (issue #2074) ---"
WFM_ACTIVE_TEST_FILE="$TEST_LOG_DIR/dispatcher-wfm-active"

# Helper: write a fresh WFM-active file (timestamp = now - $1 seconds ago)
write_wfm_active() {
    local age_seconds="$1"
    echo "$(( $(date +%s) - age_seconds ))" > "$WFM_ACTIVE_TEST_FILE"
}

remove_wfm_active() {
    rm -f "$WFM_ACTIVE_TEST_FILE"
}

# Run with WFM-active file active and a heartbeat stale for $1 seconds.
# DISPATCHER_WFM_ACTIVE_FILE is set to WFM_ACTIVE_TEST_FILE.
run_check_with_wfm() {
    local hb_stale_age="$1"
    echo "$(( $(date +%s) - hb_stale_age ))" > "$DISPATCHER_HEARTBEAT_FILE"
    DISPATCHER_WFM_ACTIVE_FILE="$WFM_ACTIVE_TEST_FILE"
    check_dispatcher_heartbeat
    local rc=$?
    DISPATCHER_WFM_ACTIVE_FILE="$TEST_LOG_DIR/dispatcher-wfm-active-ABSENT"
    return $rc
}

# -------------------------------------------------------------------
# Test 9: Stale heartbeat + WFM-active fresh + heartbeat inside cap → GREEN
# Healthy idle dispatcher: WFM is fresh, heartbeat has been stale for 1500s
# (well within the 2700s suppression cap). Should be GREEN.
# -------------------------------------------------------------------
begin_test "Stale hb (1500s) + WFM-active fresh (5s) + inside cap → GREEN"
write_wfm_active 5
run_check_with_wfm 1500 && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 10: Stale heartbeat + WFM-active fresh + heartbeat AT cap → RED
# Heartbeat stale for exactly WFM_SUPPRESSION_MAX_SECONDS: cap is hit.
# Even though WFM-active is fresh, suppression must not apply.
# -------------------------------------------------------------------
begin_test "Stale hb (at cap=${WFM_SUPPRESSION_MAX_SECONDS}s) + WFM-active fresh → RED"
write_wfm_active 5
run_check_with_wfm "$WFM_SUPPRESSION_MAX_SECONDS" && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 11: Stale heartbeat + WFM-active fresh + heartbeat beyond cap → RED
# This is the May 2026 frozen-dispatcher scenario: the daemon thread keeps
# WFM-active fresh, but the dispatcher has been unresponsive for 3600s.
# The suppression cap (2700s) has expired → RED must fire.
# -------------------------------------------------------------------
begin_test "Stale hb (3600s, beyond cap) + WFM-active fresh → RED (frozen dispatcher)"
write_wfm_active 5
run_check_with_wfm 3600 && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 12: Stale heartbeat + WFM-active also stale → RED
# Both heartbeat and WFM-active are stale: the daemon thread stopped
# updating, which means either the process died or WFM exited abnormally.
# -------------------------------------------------------------------
begin_test "Stale hb (1500s) + WFM-active stale (200s > 180s threshold) → RED"
write_wfm_active 200   # > WFM_ACTIVE_STALE_SECONDS (180s)
run_check_with_wfm 1500 && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 13: Stale heartbeat + WFM-active absent → RED
# No WFM-active file: WFM is not running (or returned normally).
# A stale heartbeat in this state is a genuine problem.
# -------------------------------------------------------------------
begin_test "Stale hb (1500s) + WFM-active absent → RED"
remove_wfm_active
run_check_with_wfm 1500 && rc=$? || rc=$?
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 14: Stale heartbeat + WFM-active tombstone ("exited") → RED
# The tombstone is written when WFM returns normally (issue #1730).
# The integer guard rejects it, so the check falls through to RED.
# -------------------------------------------------------------------
begin_test "Stale hb (1500s) + WFM-active tombstone ('exited') → RED"
echo "exited" > "$WFM_ACTIVE_TEST_FILE"
run_check_with_wfm 1500 && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 2

# -------------------------------------------------------------------
# Test 15: Stale heartbeat + WFM-active fresh + heartbeat well inside cap → GREEN
# Additional positive case: heartbeat stale for 2500s (60s below the 2700s cap
# with 60s margin to avoid timing flakiness). Should be GREEN.
# -------------------------------------------------------------------
begin_test "Stale hb (2500s) + WFM-active fresh + well inside cap → GREEN"
write_wfm_active 5
run_check_with_wfm $(( WFM_SUPPRESSION_MAX_SECONDS - 200 )) && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 0

# ===================================================================
# compute_wfm_suppression_max_seconds() derivation tests
# (issue #2074, second incident — 2026-08-03 hourly false-positive restarts)
#
# These test the pure derivation function directly: given a session-age
# limit and a margin, it returns limit - margin, with a fallback for the
# session-age-check-disabled case and a floor so it never returns <= 0.
# ===================================================================

echo ""
echo "--- compute_wfm_suppression_max_seconds() derivation tests ---"

# -------------------------------------------------------------------
# Test 16: Normal case — derives session_age_limit - margin
# -------------------------------------------------------------------
begin_test "compute_wfm_suppression_max_seconds(7200, 300, 21600) == 6900"
result=$(compute_wfm_suppression_max_seconds 7200 300 21600)
assert_eq "$result" 6900

# -------------------------------------------------------------------
# Test 17: Session-age check disabled (limit=0) → returns fallback
# -------------------------------------------------------------------
begin_test "compute_wfm_suppression_max_seconds(0, 300, 21600) == 21600 (fallback)"
result=$(compute_wfm_suppression_max_seconds 0 300 21600)
assert_eq "$result" 21600

# -------------------------------------------------------------------
# Test 18: Margin >= limit clamps to 1, never <= 0
# A cap of 0 or negative would make check_dispatcher_heartbeat() fire RED
# on every single stale-heartbeat check regardless of WFM-active — worse
# than having no cap at all. Must clamp to a positive floor.
# -------------------------------------------------------------------
begin_test "compute_wfm_suppression_max_seconds(300, 300, 21600) clamps to 1 (not 0)"
result=$(compute_wfm_suppression_max_seconds 300 300 21600)
assert_eq "$result" 1

begin_test "compute_wfm_suppression_max_seconds(100, 300, 21600) clamps to 1 (not negative)"
result=$(compute_wfm_suppression_max_seconds 100 300 21600)
assert_eq "$result" 1

# ===================================================================
# Regression tests: legitimate multi-hour idle no longer false-positives
# (issue #2074, second incident)
#
# Reproduces the exact 2026-08-03 failure mode using the production-derived
# default (SESSION_AGE_LIMIT_SECONDS=7200, margin=300 => cap=6900) instead
# of the old fixed 2700s literal, and confirms a truly stale heartbeat
# beyond the new cap still fires RED.
# ===================================================================

echo ""
echo "--- Regression: legitimate multi-hour idle no longer false-positives ---"

PRODUCTION_DEFAULT_CAP=$(compute_wfm_suppression_max_seconds 7200 300 21600)

# -------------------------------------------------------------------
# Test 19: Heartbeat stale 3000s (would have been RED under the old fixed
# 2700s cap) + WFM-active fresh → GREEN under the derived default.
# This is the exact scenario that paged the user hourly on 2026-08-03: a
# healthy dispatcher idling in wait_for_messages for ~50 minutes.
# -------------------------------------------------------------------
begin_test "Stale hb (3000s, RED under old 2700s cap) + WFM-active fresh → GREEN under derived cap (${PRODUCTION_DEFAULT_CAP}s)"
WFM_SUPPRESSION_MAX_SECONDS="$PRODUCTION_DEFAULT_CAP"
write_wfm_active 5
run_check_with_wfm 3000 && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 0

# -------------------------------------------------------------------
# Test 20: Heartbeat stale beyond the derived cap + WFM-active fresh →
# still RED. The cap still catches a truly frozen dispatcher; it is just
# no longer tighter than the session lifetime a legitimate idle wait is
# allowed to reach.
# -------------------------------------------------------------------
begin_test "Stale hb (beyond derived cap ${PRODUCTION_DEFAULT_CAP}s) + WFM-active fresh → RED"
WFM_SUPPRESSION_MAX_SECONDS="$PRODUCTION_DEFAULT_CAP"
write_wfm_active 5
run_check_with_wfm $(( PRODUCTION_DEFAULT_CAP + 100 )) && rc=$? || rc=$?
remove_wfm_active
assert_exit "$rc" 2

# Restore the mechanism-test cap for anything appended after this point.
WFM_SUPPRESSION_MAX_SECONDS=2700

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
echo ""
echo "Results: $PASS/$TOTAL passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
