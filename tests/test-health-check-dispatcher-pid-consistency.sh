#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Dispatcher PID Consistency (issue #2148, Phase 1)
#
# Tests for check_dispatcher_pid_consistency() — the additive, observational-only
# cross-check that reads the `pid` column session_start() now writes for the
# dispatcher's own agent_sessions.db row and compares it against a real `pgrep -x
# claude` scan / kill -0 liveness probe.
#
# This check NEVER sets `level`/`restart_reason` (it must not duplicate or
# override check_wrapper_process / check_claude_process, which remain the
# authoritative, independent liveness checks) — it always returns 0 and only
# logs. These tests assert on log output and return code only.
#
# Cases covered:
#   1. No agent_sessions.db file at all → returns 0, no crash (pre-install / fresh instance)
#   2. DB exists but no dispatcher row has a non-NULL pid (pre-migration row) → info log, returns 0
#   3. DB has a dispatcher pid that IS alive and IS among pgrep's "claude" results → consistent, info log
#   4. DB has a dispatcher pid that is NOT alive (kill -0 fails) → warn log, returns 0 (non-fatal)
#   5. DB has a dispatcher pid that IS alive but NOT among pgrep's "claude" results → warn log, returns 0
#
# Usage: bash tests/test-health-check-dispatcher-pid-consistency.sh
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

TEST_TMPDIR=$(mktemp -d /tmp/lobster-dispatcher-pid-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_exit() {
    local actual="$1" expected="$2"
    if [[ "$actual" -eq "$expected" ]]; then pass; else fail "expected exit $expected, got $actual"; fi
}

assert_log_contains() {
    local needle="$1"
    if grep -qF "$needle" "$LOG_FILE" 2>/dev/null; then
        pass
    else
        fail "log did not contain: $needle (log: $(cat "$LOG_FILE" 2>/dev/null))"
    fi
}

LOG_FILE="$TEST_TMPDIR/health-check.log"
log()       { echo "[$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

# Load the function definition verbatim from the health check script (same
# extraction pattern as test-health-check-dispatcher-heartbeat.sh — avoids
# hand-copied duplication drifting out of sync with the real implementation).
eval "$(sed -n '/^check_dispatcher_pid_consistency()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

# Build a fresh agent_sessions.db with a single dispatcher row.
# Args: $1 = db path, $2 = pid value (or "NULL")
make_dispatcher_db() {
    local db_path="$1"
    local pid_value="$2"
    sqlite3 "$db_path" <<SQL
CREATE TABLE agent_sessions (
    id TEXT, agent_type TEXT, status TEXT, spawned_at TEXT, pid INTEGER
);
INSERT INTO agent_sessions (id, agent_type, status, spawned_at, pid)
VALUES ('lobster-dispatcher', 'dispatcher', 'running', '2026-08-04T00:00:00+00:00', ${pid_value});
SQL
}

echo "=== Dispatcher PID Consistency Health Check Tests ==="
echo ""

# -------------------------------------------------------------------
# Test 1: No DB file at all → returns 0, no crash
# -------------------------------------------------------------------
begin_test "No agent_sessions.db file → returns 0 (fail-open)"
: > "$LOG_FILE"
MESSAGES_DIR="$TEST_TMPDIR/no-db-messages"
mkdir -p "$MESSAGES_DIR/config"
check_dispatcher_pid_consistency
assert_exit "$?" 0

# -------------------------------------------------------------------
# Test 2: DB exists, dispatcher row has NULL pid (pre-migration) → info log, returns 0
# -------------------------------------------------------------------
begin_test "Dispatcher row with NULL pid → info log, returns 0"
: > "$LOG_FILE"
MESSAGES_DIR="$TEST_TMPDIR/null-pid-messages"
mkdir -p "$MESSAGES_DIR/config"
make_dispatcher_db "$MESSAGES_DIR/config/agent_sessions.db" "NULL"
check_dispatcher_pid_consistency
assert_exit "$?" 0
begin_test "Dispatcher row with NULL pid → logs 'no pid recorded'"
assert_log_contains "no pid recorded"

# -------------------------------------------------------------------
# Test 3: DB pid alive AND among pgrep's claude results → consistent, info log
# -------------------------------------------------------------------
begin_test "Live pid matching pgrep claude list → consistent info log"
: > "$LOG_FILE"
MESSAGES_DIR="$TEST_TMPDIR/consistent-messages"
mkdir -p "$MESSAGES_DIR/config"
make_dispatcher_db "$MESSAGES_DIR/config/agent_sessions.db" "42424"
kill() { [[ "$1" == "-0" ]] && return 0; }
pgrep() { echo "42424"; return 0; }
check_dispatcher_pid_consistency
assert_exit "$?" 0
begin_test "Live pid matching pgrep claude list → logs 'consistent'"
assert_log_contains "consistent"
unset -f kill pgrep

# -------------------------------------------------------------------
# Test 4: DB pid not alive (kill -0 fails) → warn log, returns 0 (non-fatal)
# -------------------------------------------------------------------
begin_test "Dead pid (kill -0 fails) → warn log, returns 0"
: > "$LOG_FILE"
MESSAGES_DIR="$TEST_TMPDIR/dead-pid-messages"
mkdir -p "$MESSAGES_DIR/config"
make_dispatcher_db "$MESSAGES_DIR/config/agent_sessions.db" "99999"
kill() { return 1; }
check_dispatcher_pid_consistency
assert_exit "$?" 0
begin_test "Dead pid (kill -0 fails) → logs 'not alive'"
assert_log_contains "not alive"
unset -f kill

# -------------------------------------------------------------------
# Test 5: DB pid alive but NOT among pgrep's claude results → warn log, returns 0
# -------------------------------------------------------------------
begin_test "Live pid absent from pgrep claude list → warn log, returns 0"
: > "$LOG_FILE"
MESSAGES_DIR="$TEST_TMPDIR/mismatch-messages"
mkdir -p "$MESSAGES_DIR/config"
make_dispatcher_db "$MESSAGES_DIR/config/agent_sessions.db" "55555"
kill() { [[ "$1" == "-0" ]] && return 0; }
pgrep() { echo "11111"; return 0; }
check_dispatcher_pid_consistency
assert_exit "$?" 0
begin_test "Live pid absent from pgrep claude list → logs mismatch warning"
assert_log_contains "is not among the 'claude'-named processes"
unset -f kill pgrep

# -------------------------------------------------------------------
# Test 6: level/restart_reason are never touched by this check
# -------------------------------------------------------------------
begin_test "Function never sets level or restart_reason (grep of source)"
if grep -A 40 '^check_dispatcher_pid_consistency()' "$HEALTH_SCRIPT" | sed -n '1,/^}/p' | grep -qE 'level=|restart_reason='; then
    fail "check_dispatcher_pid_consistency() must never assign level or restart_reason"
else
    pass
fi

#===============================================================================
# Syntax check
#===============================================================================

echo ""
echo "=== Syntax Check ==="

begin_test "health-check-v3.sh passes bash -n syntax check"
if bash -n "$HEALTH_SCRIPT" 2>&1; then
    pass
else
    fail "Syntax errors in health-check-v3.sh"
fi

#===============================================================================
# Summary
#===============================================================================

echo ""
echo "=============================="
echo "Results: $TOTAL tests"
echo -e "  ${GREEN}PASS: $PASS${NC}"
if [ "$FAIL" -gt 0 ]; then
    echo -e "  ${RED}FAIL: $FAIL${NC}"
fi
echo "=============================="

if [ "$FAIL" -gt 0 ]; then
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
