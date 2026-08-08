#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - count_active_subagents() dispatcher exclusion
# (issue #2176, Phase 3 — pre-existing bug found as a byproduct of the
# ghost-kill regression investigation, NOT caused by PR #2152)
#
# count_active_subagents() feeds do_restart()'s SUBAGENT GUARD
# (`if [[ "$active_subagents" -gt 0 ]]`), which defers a RED-state restart
# whenever the count is nonzero. Before this fix, the query had zero
# dispatcher exclusion: `SELECT COUNT(*) FROM agent_sessions WHERE
# status='running'`. Since the dispatcher's own row is always status='running'
# for its entire lifetime, this count structurally always included at least 1
# for the dispatcher itself — meaning the guard would defer every RED-state
# restart indefinitely whenever the dispatcher's row was present, regardless
# of whether any real subagent was actually running.
#
# Fix applies DISPATCHER_EXCLUSION_SQL (scripts/lib/agent_sessions.sh), the
# same shared pattern used by periodic-self-check.sh's PENDING_COUNT query
# and src/agents/session_store.py's Python-side queries.
#
# Cases covered:
#   1. Only a dispatcher row (status='running') → count is 0, not 1
#   2. Dispatcher row + one real running subagent → count is 1, not 2
#   3. No agent_sessions.db file → count is 0 (fail-open, unaffected by fix)
#   4. Two real subagents, no dispatcher row → count is 2 (unaffected by fix)
#
# Usage: bash tests/test-health-check-count-active-subagents.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$REPO_ROOT/scripts"
HEALTH_SCRIPT="$SCRIPT_DIR/health-check-v3.sh"

# count_active_subagents() sources "${LOBSTER_INSTALL_DIR:-$HOME/lobster}/scripts/lib/agent_sessions.sh"
# — point it at this checkout regardless of whether ~/lobster matches.
export LOBSTER_INSTALL_DIR="$REPO_ROOT"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-count-active-subagents-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected '$expected', got '$actual'"; fi
}

# Load the function definition verbatim from the health check script (same
# extraction pattern as test-health-check-dispatcher-pid-consistency.sh —
# avoids hand-copied duplication drifting out of sync with the real
# implementation).
eval "$(sed -n '/^count_active_subagents()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

# Build an agent_sessions.db with the given rows.
# Args: $1 = db path, remaining = "id|agent_type|status" triples
make_db() {
    local db_path="$1"
    shift
    sqlite3 "$db_path" "CREATE TABLE agent_sessions (id TEXT, agent_type TEXT, status TEXT);"
    for row in "$@"; do
        IFS='|' read -r id agent_type status <<< "$row"
        sqlite3 "$db_path" "INSERT INTO agent_sessions (id, agent_type, status) VALUES ('$id', '$agent_type', '$status');"
    done
}

echo "=== count_active_subagents() Dispatcher Exclusion Tests ==="
echo ""

# -------------------------------------------------------------------
# Test 1: Only a dispatcher row → count is 0, not 1
# -------------------------------------------------------------------
begin_test "Only dispatcher row (status=running) → count is 0"
MESSAGES_DIR="$TEST_TMPDIR/dispatcher-only"
mkdir -p "$MESSAGES_DIR/config"
make_db "$MESSAGES_DIR/config/agent_sessions.db" "lobster-dispatcher|dispatcher|running"
result=$(count_active_subagents)
assert_eq "$result" "0"

# -------------------------------------------------------------------
# Test 2: Dispatcher row + one real running subagent → count is 1, not 2
# -------------------------------------------------------------------
begin_test "Dispatcher row + 1 real subagent → count is 1"
MESSAGES_DIR="$TEST_TMPDIR/dispatcher-plus-one"
mkdir -p "$MESSAGES_DIR/config"
make_db "$MESSAGES_DIR/config/agent_sessions.db" \
    "lobster-dispatcher|dispatcher|running" \
    "real-subagent-001|subagent|running"
result=$(count_active_subagents)
assert_eq "$result" "1"

# -------------------------------------------------------------------
# Test 3: No agent_sessions.db file → count is 0 (fail-open, unaffected by fix)
# -------------------------------------------------------------------
begin_test "No agent_sessions.db file → count is 0 (fail-open)"
MESSAGES_DIR="$TEST_TMPDIR/no-db"
mkdir -p "$MESSAGES_DIR/config"
result=$(count_active_subagents)
assert_eq "$result" "0"

# -------------------------------------------------------------------
# Test 4: Two real subagents, no dispatcher row → count is 2 (unaffected)
# -------------------------------------------------------------------
begin_test "Two real subagents, no dispatcher row → count is 2"
MESSAGES_DIR="$TEST_TMPDIR/two-subagents"
mkdir -p "$MESSAGES_DIR/config"
make_db "$MESSAGES_DIR/config/agent_sessions.db" \
    "real-subagent-001|subagent|running" \
    "real-subagent-002|subagent|running"
result=$(count_active_subagents)
assert_eq "$result" "2"

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
