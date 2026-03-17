#!/bin/bash
#===============================================================================
# Smoke Test: Self-check output is bounded
#
# Verifies that scan_completed_tasks() and scan_agent_status() never produce
# output exceeding the cap, regardless of how many agent files exist.
#
# This directly tests the fix for issue #593, where 315+ completed agents
# caused ~10k-token self-check messages every 3 minutes.
#
# Usage: bash tests/smoke/test_selfcheck_bounded.sh
#===============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts"
AGENT_STATUS_SCRIPT="$SCRIPT_DIR/agent-status.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-smoke-bounded-XXXXXX)
TEST_TASKS_DIR="$TEST_TMPDIR/tasks"
TEST_STATE_DIR="$TEST_TMPDIR/state"

cleanup() {
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

mkdir -p "$TEST_TASKS_DIR" "$TEST_STATE_DIR"

begin_test() {
    test_name="$1"
    TOTAL=$((TOTAL + 1))
}

pass() {
    PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} $test_name"
}

fail() {
    FAIL=$((FAIL + 1))
    local msg="${1:-}"
    echo -e "  ${RED}FAIL${NC} $test_name${msg:+: $msg}"
}

# Create a fake agent output file with stop_reason=end_turn (completed)
create_completed_agent() {
    local name="$1"
    local turns="${2:-5}"
    local filepath="$TEST_TASKS_DIR/${name}.output"
    for ((i = 1; i <= turns; i++)); do
        echo '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"turn '"$i"'"}]}}' >> "$filepath"
    done
    # Write terminal stop_reason
    echo '{"type":"result","stop_reason":"end_turn"}' >> "$filepath"
}

# Create a fake agent output file with stop_reason=tool_use (running)
create_running_agent() {
    local name="$1"
    local turns="${2:-5}"
    local filepath="$TEST_TASKS_DIR/${name}.output"
    for ((i = 1; i <= turns; i++)); do
        echo '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"turn '"$i"'"}]}}' >> "$filepath"
    done
    # Write running stop_reason
    echo '{"type":"result","stop_reason":"tool_use"}' >> "$filepath"
}

echo ""
echo -e "${BOLD}=== Smoke: Self-check output bounded (issue #593) ===${NC}"

#-------------------------------------------------------------------------------
# Test 1: scan_completed_tasks() caps at 3 when 315 completed tasks exist
#-------------------------------------------------------------------------------
begin_test "scan_completed_tasks caps at COMPLETED_MAX_REPORT=3 with 315 completed tasks"

rm -f "$TEST_TASKS_DIR"/*.output
for i in $(seq 1 315); do
    create_completed_agent "completed_$(printf '%04d' "$i")" 10
done

export AGENT_TASKS_DIR="$TEST_TASKS_DIR"
export AGENT_STATE_DIR="$TEST_STATE_DIR"
source "$AGENT_STATUS_SCRIPT"

RESULT=$(scan_completed_tasks)

# Count how many "Task ... completed" entries appear
ENTRY_COUNT=$(echo "$RESULT" | grep -o "Task completed_" | wc -l)
if [ "$ENTRY_COUNT" -le 3 ]; then
    pass
else
    fail "Expected <= 3 entries, got $ENTRY_COUNT. Output length: ${#RESULT} chars"
fi

#-------------------------------------------------------------------------------
# Test 2: All 315 overflow tasks are batch-marked as reported (no re-reporting)
#-------------------------------------------------------------------------------
begin_test "All overflow tasks marked reported — second call returns empty"

RESULT2=$(scan_completed_tasks)
if [ -z "$RESULT2" ]; then
    pass
else
    ENTRY_COUNT2=$(echo "$RESULT2" | grep -o "Task completed_" | wc -l)
    fail "Expected empty on second call, got $ENTRY_COUNT2 entries"
fi

#-------------------------------------------------------------------------------
# Test 3: Output character length is bounded (no transcript content included)
#-------------------------------------------------------------------------------
begin_test "scan_completed_tasks output length is bounded (< 500 chars for 3 entries)"

rm -f "$TEST_STATE_DIR"/reported-tasks
rm -f "$TEST_TASKS_DIR"/*.output

# Create 3 completed tasks with long names
for i in 1 2 3; do
    create_completed_agent "a-very-long-task-id-name-for-agent-${i}-test" 50
done

export AGENT_STATE_DIR="$TEST_STATE_DIR"
RESULT=$(scan_completed_tasks)
CHAR_LEN=${#RESULT}

if [ "$CHAR_LEN" -lt 500 ]; then
    pass
else
    fail "Output too long: $CHAR_LEN chars (expected < 500)"
fi

#-------------------------------------------------------------------------------
# Test 4: scan_agent_status() excludes completed agents
#-------------------------------------------------------------------------------
begin_test "scan_agent_status excludes completed (end_turn) agents entirely"

rm -f "$TEST_TASKS_DIR"/*.output
rm -f "$TEST_STATE_DIR"/reported-tasks

# Create 10 completed agents and 2 running ones
for i in $(seq 1 10); do
    create_completed_agent "done_agent_${i}" 5
done
create_running_agent "active_agent_1" 3
create_running_agent "active_agent_2" 7

export AGENT_TASKS_DIR="$TEST_TASKS_DIR"
export AGENT_STATE_DIR="$TEST_STATE_DIR"
source "$AGENT_STATUS_SCRIPT"

RESULT=$(scan_agent_status)

# Must not contain any done_agent entries
if echo "$RESULT" | grep -q "done_agent"; then
    fail "Completed agents found in scan_agent_status output: '$RESULT'"
elif [[ "$RESULT" == *"active_agent"* ]]; then
    pass
else
    fail "Expected active agents in output, got: '$RESULT'"
fi

#-------------------------------------------------------------------------------
# Test 5: scan_agent_status returns empty when all agents are done
#-------------------------------------------------------------------------------
begin_test "scan_agent_status returns empty string when all agents are completed"

rm -f "$TEST_TASKS_DIR"/*.output

for i in $(seq 1 20); do
    create_completed_agent "only_done_${i}" 5
done

export AGENT_TASKS_DIR="$TEST_TASKS_DIR"
source "$AGENT_STATUS_SCRIPT"

RESULT=$(scan_agent_status)
if [ -z "$RESULT" ]; then
    pass
else
    fail "Expected empty, got: '$RESULT'"
fi

#-------------------------------------------------------------------------------
# Test 6: scan_agent_status caps at AGENT_MAX_DISPLAY=5 running agents
#-------------------------------------------------------------------------------
begin_test "scan_agent_status caps at 5 running agents (reports +N more for overflow)"

rm -f "$TEST_TASKS_DIR"/*.output

for i in $(seq 1 8); do
    create_running_agent "run_agent_${i}" 3
done

export AGENT_TASKS_DIR="$TEST_TASKS_DIR"
source "$AGENT_STATUS_SCRIPT"

RESULT=$(scan_agent_status)
ENTRY_COUNT=$(echo "$RESULT" | tr ',' '\n' | grep -c "turns" || echo 0)

if [ "$ENTRY_COUNT" -le 5 ] && [[ "$RESULT" == *"+3 more"* ]]; then
    pass
elif [ "$ENTRY_COUNT" -le 5 ]; then
    fail "Capped correctly at $ENTRY_COUNT but missing '+3 more': '$RESULT'"
else
    fail "Expected <= 5 entries, got $ENTRY_COUNT"
fi

#===============================================================================
# Summary
#===============================================================================

echo ""
echo -e "${BOLD}==============================${NC}"
echo -e "${BOLD}Results: $TOTAL tests${NC}"
echo -e "  ${GREEN}PASS: $PASS${NC}"
if [ "$FAIL" -gt 0 ]; then
    echo -e "  ${RED}FAIL: $FAIL${NC}"
fi
echo -e "${BOLD}==============================${NC}"

if [ "$FAIL" -gt 0 ]; then
    exit 1
else
    echo -e "${GREEN}All bounded-output smoke tests passed!${NC}"
    exit 0
fi
