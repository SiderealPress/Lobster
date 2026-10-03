#!/bin/bash
#===============================================================================
# Test Suite: periodic-self-check.sh PENDING_COUNT — dispatcher row with NULL
# agent_type (issue #2226)
#
# periodic-self-check.sh (cron, every 3 min) counts "pending" agents via:
#   SELECT COUNT(*) FROM agent_sessions
#   WHERE status IN ('running','starting') AND $DISPATCHER_EXCLUSION_SQL
# (scripts/lib/agent_sessions.sh's DISPATCHER_EXCLUSION_SQL, sourced directly
# by the script — this test exercises that exact query string, not a
# reimplementation of it).
#
# The dispatcher registers its own row via session_start(agent_id=
# "lobster-dispatcher", ...). agent_type is an OPTIONAL parameter on that
# call — when the dispatcher's bootup turn omits it, the row's agent_type
# column lands NULL. Pre-fix, DISPATCHER_EXCLUSION_SQL was just
# `COALESCE(agent_type, '') != 'dispatcher'`, which evaluates to
# `'' != 'dispatcher'` -> TRUE for a NULL row, so it was NOT excluded — every
# 3-minute cron cycle counted it as 1 pending agent and injected a spurious
# "status? (Self-check) [1 agents pending]" message, indefinitely, with zero
# real subagents running. Confirmed live against the production DB in issue
# #2226's bug report.
#
# Fix: DISPATCHER_EXCLUSION_SQL now also excludes by the dispatcher's static
# agent_id ('lobster-dispatcher') as a second, independent signal — mirroring
# the belt-and-suspenders pattern scripts/agent-monitor.py's
# _is_dispatcher_agent() already used for its own call site (issue #2176).
#
# Cases covered:
#   1. Dispatcher row, agent_type=NULL (the exact reported bug) -> count is 0
#   2. Dispatcher row (agent_type=NULL) + one real pending subagent -> count is 1
#   3. Dispatcher row, agent_type='dispatcher' (already-working case) -> count is 0
#   4. No dispatcher row, one real pending subagent -> count is 1 (unaffected)
#
# Usage: bash tests/test-periodic-self-check-dispatcher-null-agent-type.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-periodic-self-check-null-agent-type-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected '$expected', got '$actual'"; fi
}

# Source the real shared fragment — the same single source of truth
# periodic-self-check.sh itself sources. Not a hand-copied reimplementation.
# shellcheck source=../scripts/lib/agent_sessions.sh
source "$REPO_ROOT/scripts/lib/agent_sessions.sh"

# Build an agent_sessions.db with the given rows.
# Args: $1 = db path, remaining = "id|agent_type|status" triples.
# agent_type of the literal string "NULL" is inserted as SQL NULL (unquoted).
make_db() {
    local db_path="$1"
    shift
    sqlite3 "$db_path" "CREATE TABLE agent_sessions (id TEXT PRIMARY KEY, agent_type TEXT, status TEXT);"
    for row in "$@"; do
        IFS='|' read -r id agent_type status <<< "$row"
        if [[ "$agent_type" == "NULL" ]]; then
            sqlite3 "$db_path" "INSERT INTO agent_sessions (id, agent_type, status) VALUES ('$id', NULL, '$status');"
        else
            sqlite3 "$db_path" "INSERT INTO agent_sessions (id, agent_type, status) VALUES ('$id', '$agent_type', '$status');"
        fi
    done
}

# Run the exact PENDING_COUNT query periodic-self-check.sh runs.
pending_count() {
    local db_path="$1"
    sqlite3 "$db_path" \
        "SELECT COUNT(*) FROM agent_sessions WHERE status IN ('running','starting') AND ${DISPATCHER_EXCLUSION_SQL}" \
        2>/dev/null || echo "0"
}

echo "=== periodic-self-check.sh PENDING_COUNT — Dispatcher NULL agent_type Tests (issue #2226) ==="
echo ""

# -------------------------------------------------------------------
# Test 1: The exact reported bug — dispatcher row, agent_type=NULL
# -------------------------------------------------------------------
begin_test "Dispatcher row (agent_type=NULL) -> count is 0, not 1"
DB="$TEST_TMPDIR/null-agent-type.db"
make_db "$DB" "lobster-dispatcher|NULL|running"
result=$(pending_count "$DB")
assert_eq "$result" "0"

# -------------------------------------------------------------------
# Test 2: Dispatcher row (agent_type=NULL) + one real pending subagent
# -------------------------------------------------------------------
begin_test "Dispatcher row (agent_type=NULL) + 1 real subagent -> count is 1"
DB="$TEST_TMPDIR/null-agent-type-plus-one.db"
make_db "$DB" \
    "lobster-dispatcher|NULL|running" \
    "real-subagent-001|subagent|running"
result=$(pending_count "$DB")
assert_eq "$result" "1"

# -------------------------------------------------------------------
# Test 3: Dispatcher row with agent_type correctly set (already-working
# case) -> must remain excluded (no regression from the id fallback)
# -------------------------------------------------------------------
begin_test "Dispatcher row (agent_type='dispatcher') -> count is 0"
DB="$TEST_TMPDIR/correct-agent-type.db"
make_db "$DB" "lobster-dispatcher|dispatcher|running"
result=$(pending_count "$DB")
assert_eq "$result" "0"

# -------------------------------------------------------------------
# Test 4: No dispatcher row, one real pending subagent -> count is 1
# -------------------------------------------------------------------
begin_test "No dispatcher row, 1 real subagent -> count is 1 (unaffected)"
DB="$TEST_TMPDIR/no-dispatcher-row.db"
make_db "$DB" "real-subagent-001|subagent|running"
result=$(pending_count "$DB")
assert_eq "$result" "1"

#===============================================================================
# Syntax check
#===============================================================================

echo ""
echo "=== Syntax Check ==="

begin_test "periodic-self-check.sh passes bash -n syntax check"
if bash -n "$REPO_ROOT/scripts/periodic-self-check.sh" 2>&1; then
    pass
else
    fail "Syntax errors in periodic-self-check.sh"
fi

begin_test "scripts/lib/agent_sessions.sh passes bash -n syntax check"
if bash -n "$REPO_ROOT/scripts/lib/agent_sessions.sh" 2>&1; then
    pass
else
    fail "Syntax errors in scripts/lib/agent_sessions.sh"
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
