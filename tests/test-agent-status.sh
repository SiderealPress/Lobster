#!/bin/bash
#===============================================================================
# Test Suite: Agent Status Scanning for Self-Check Messages
#
# Tests the scan_agent_status() function that examines background agent
# output files and produces a concise status summary.
#
# Usage: bash tests/test-agent-status.sh
#===============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# Counters
PASS=0
FAIL=0
SKIP=0
TOTAL=0

# Script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
AGENT_STATUS_SCRIPT="$SCRIPT_DIR/agent-status.sh"

# Test isolation
TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-agent-XXXXXX)
TEST_TASKS_DIR="$TEST_TMPDIR/tasks"

cleanup() {
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

mkdir -p "$TEST_TASKS_DIR"

#===============================================================================
# Test Helpers
#===============================================================================

test_name=""

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
    if [ -n "$msg" ]; then
        echo -e "  ${RED}FAIL${NC} $test_name: $msg"
    else
        echo -e "  ${RED}FAIL${NC} $test_name"
    fi
}

skip() {
    SKIP=$((SKIP + 1))
    local msg="${1:-}"
    if [ -n "$msg" ]; then
        echo -e "  ${YELLOW}SKIP${NC} $test_name: $msg"
    else
        echo -e "  ${YELLOW}SKIP${NC} $test_name"
    fi
}

reset_tasks() {
    rm -f "$TEST_TASKS_DIR"/*
}

# Create a fake agent output file with N assistant turns
# Args: $1=filename, $2=num_assistant_turns, $3=mtime_seconds_ago (optional, default 0)
create_agent_file() {
    local filename="$1"
    local turns="$2"
    local age_seconds="${3:-0}"
    local filepath="$TEST_TASKS_DIR/$filename"

    > "$filepath"
    for ((i = 1; i <= turns; i++)); do
        echo '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"turn '"$i"'"}]}}' >> "$filepath"
        # Add some non-assistant lines too (tool use, user, progress)
        echo '{"type":"user","message":{"role":"user","content":[{"type":"tool_result"}]}}' >> "$filepath"
        echo '{"type":"progress","data":{"type":"hook_progress"}}' >> "$filepath"
    done

    if [ "$age_seconds" -gt 0 ]; then
        touch -d "$age_seconds seconds ago" "$filepath"
    fi
}

# Source the function to test
source_agent_status() {
    export AGENT_TASKS_DIR="$TEST_TASKS_DIR"
    source "$AGENT_STATUS_SCRIPT"
}

#===============================================================================
# Tests: agent-status.sh
#===============================================================================

echo ""
echo -e "${BOLD}=== agent-status.sh (agent scanning) ===${NC}"

# Test 1: Script exists and is syntactically valid
begin_test "agent-status.sh passes bash -n syntax check"
if bash -n "$AGENT_STATUS_SCRIPT" 2>/dev/null; then
    pass
else
    fail "Syntax error in agent-status.sh"
fi

# Test 2: No agent files -> empty result
begin_test "Returns empty string when no agent output files exist"
reset_tasks
source_agent_status
RESULT=$(scan_agent_status)
if [ -z "$RESULT" ]; then
    pass
else
    fail "Expected empty, got: '$RESULT'"
fi

# Test 3: Single active agent
begin_test "Reports single active agent with turn count"
reset_tasks
create_agent_file "abc1234.output" 10 5
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"abc1234"* ]] && [[ "$RESULT" == *"10 turns"* ]]; then
    pass
else
    fail "Expected 'abc1234' and '10 turns' in: '$RESULT'"
fi

# Test 4: Active agent shows "last activity Xs ago"
begin_test "Active agent shows last activity time"
reset_tasks
create_agent_file "def5678.output" 5 30
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"last activity"* ]]; then
    pass
else
    fail "Expected 'last activity' in: '$RESULT'"
fi

# Test 5: Stale agent (15+ minutes old)
begin_test "Agent with 15+ min old mtime shows as stale"
reset_tasks
create_agent_file "stale123.output" 20 1000
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"stale"* ]]; then
    pass
else
    fail "Expected 'stale' in: '$RESULT'"
fi

# Test 6: Stale agent shows time in human-readable format
begin_test "Stale agent shows time in minutes or hours"
reset_tasks
create_agent_file "stale456.output" 30 7200
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"stale 2h"* ]]; then
    pass
else
    fail "Expected 'stale 2h' in: '$RESULT'"
fi

# Test 7: Multiple agents
begin_test "Reports multiple agents separated by comma"
reset_tasks
create_agent_file "agent_a.output" 10 5
create_agent_file "agent_b.output" 20 900
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"agent_a"* ]] && [[ "$RESULT" == *"agent_b"* ]]; then
    pass
else
    fail "Expected both agents in: '$RESULT'"
fi

# Test 8: Turn count is accurate (counts only "type":"assistant" lines)
begin_test "Turn count only counts assistant-type lines"
reset_tasks
# create_agent_file creates 3 lines per turn (assistant, user, progress)
# so 5 turns = 15 lines, but only 5 should be counted
create_agent_file "count_test.output" 5 5
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"5 turns"* ]]; then
    pass
else
    fail "Expected '5 turns' in: '$RESULT'"
fi

# Test 9: Stale threshold is 15 minutes (900 seconds)
begin_test "Agent at exactly 14 minutes is NOT stale"
reset_tasks
create_agent_file "border.output" 10 840
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"last activity"* ]] && [[ "$RESULT" != *"stale"* ]]; then
    pass
else
    fail "Expected 'last activity' (not stale) in: '$RESULT'"
fi

# Test 10: Agent at 16 minutes IS stale
begin_test "Agent at 16 minutes IS stale"
reset_tasks
create_agent_file "border2.output" 10 960
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"stale"* ]]; then
    pass
else
    fail "Expected 'stale' in: '$RESULT'"
fi

# Test 11: Zero-turn file (empty or no assistant lines)
begin_test "File with zero assistant turns shows 0 turns"
reset_tasks
echo '{"type":"user","message":{}}' > "$TEST_TASKS_DIR/empty_agent.output"
touch -d "10 seconds ago" "$TEST_TASKS_DIR/empty_agent.output"
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"0 turns"* ]]; then
    pass
else
    fail "Expected '0 turns' in: '$RESULT'"
fi

# Test 12: Non-.output files are ignored
begin_test "Non-.output files are ignored"
reset_tasks
echo '{"type":"assistant"}' > "$TEST_TASKS_DIR/abc123.log"
echo '{"type":"assistant"}' > "$TEST_TASKS_DIR/abc123.json"
source_agent_status
RESULT=$(scan_agent_status)
if [ -z "$RESULT" ]; then
    pass
else
    fail "Expected empty, got: '$RESULT'"
fi

# Test 13: Many agents - capped to avoid huge messages
begin_test "Caps at 5 agents when many exist"
reset_tasks
for i in $(seq 1 10); do
    create_agent_file "agent_$(printf '%02d' "$i").output" "$i" 5
done
source_agent_status
RESULT=$(scan_agent_status)
# Count how many agent entries are in the result
AGENT_COUNT=$(echo "$RESULT" | tr ',' '\n' | grep -c "turns" || echo 0)
if [ "$AGENT_COUNT" -le 5 ]; then
    pass
else
    fail "Expected at most 5 agents, got $AGENT_COUNT in: '$RESULT'"
fi

# Test 14: Shows "+N more" when agents are capped
begin_test "Shows +N more when agents exceed cap"
reset_tasks
for i in $(seq 1 8); do
    create_agent_file "many_$(printf '%02d' "$i").output" "$i" 5
done
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"+3 more"* ]]; then
    pass
else
    fail "Expected '+3 more' in: '$RESULT'"
fi

# Test 15: Human-readable time formatting
begin_test "Time formatting: seconds for < 60s"
reset_tasks
create_agent_file "time_s.output" 5 30
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"30s"* ]]; then
    pass
else
    fail "Expected '30s' in: '$RESULT'"
fi

# Test 16: Time formatting for minutes
begin_test "Time formatting: minutes for 60s-3600s"
reset_tasks
create_agent_file "time_m.output" 5 300
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"5m"* ]]; then
    pass
else
    fail "Expected '5m' in: '$RESULT'"
fi

# Test 17: Time formatting for hours
begin_test "Time formatting: hours for > 3600s"
reset_tasks
create_agent_file "time_h.output" 5 7200
source_agent_status
RESULT=$(scan_agent_status)
if [[ "$RESULT" == *"2h"* ]]; then
    pass
else
    fail "Expected '2h' in: '$RESULT'"
fi

# Test 18: Full message format matches spec
begin_test "Full output format matches 'Agents: id (N turns, status)' pattern"
reset_tasks
create_agent_file "fmt_test.output" 15 10
source_agent_status
RESULT=$(scan_agent_status)
# Should match: "Agents: fmt_test (15 turns, last activity 10s ago)"
if [[ "$RESULT" =~ ^Agents:\ .+\([0-9]+\ turns,\ (last\ activity|stale)\ [0-9]+[smh]( ago)?\)$ ]]; then
    pass
else
    fail "Format mismatch: '$RESULT'"
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
if [ "$SKIP" -gt 0 ]; then
    echo -e "  ${YELLOW}SKIP: $SKIP${NC}"
fi
echo -e "${BOLD}==============================${NC}"

if [ "$FAIL" -gt 0 ]; then
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
