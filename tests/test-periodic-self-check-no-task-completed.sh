#!/bin/bash
#===============================================================================
# Test Suite: periodic-self-check.sh does not emit "[Task Completed]" messages
#
# Subagents report completion through write_result, which writes a
# subagent_result file into the inbox; the SubagentStop hook
# require-write-result.py enforces that call. A second "[Task Completed]"
# message built from Claude Code task output files (scan_completed_tasks) is a
# duplicate, lower-fidelity notification of the same event, so the self-check
# must not inject one. The "status?" ping for still-running subagents stays.
#
# The script is run end to end as a subprocess against a fully synthetic
# environment: temp HOME / LOBSTER_MESSAGES / LOBSTER_INSTALL_DIR /
# LOBSTER_WORKSPACE / LOBSTER_CONFIG_DIR, a fixture tasks dir
# (AGENT_TASKS_DIR), a fixture agent_sessions.db, and a PATH-stubbed `pgrep`
# so Guard 1 ("is a Claude Code process running?") passes deterministically.
#
# Cases covered:
#   1. Script passes `bash -n`
#   2. Unreported end_turn task output + a running subagent row
#      -> no "[Task Completed]" inbox message; a plain "status?" message instead
#   3. Unreported end_turn task output + no running subagent rows
#      -> no inbox message at all (completion is write_result's job)
#   4. The script no longer records task ids in the reported-tasks state file
#
# Usage: bash tests/test-periodic-self-check-no-task-completed.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SELF_CHECK_SCRIPT="$REPO_ROOT/scripts/periodic-self-check.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-self-check-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

test_name=""
begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass() { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail() { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

#===============================================================================
# Fixture helpers
#===============================================================================

# Build an isolated environment for one case. Prints the case root.
# Layout:
#   <root>/home                     HOME
#   <root>/messages/inbox           LOBSTER_MESSAGES inbox
#   <root>/messages/config/agent_sessions.db
#   <root>/install/scripts -> repo scripts (LOBSTER_INSTALL_DIR; .state lives here)
#   <root>/workspace                LOBSTER_WORKSPACE
#   <root>/config                   LOBSTER_CONFIG_DIR
#   <root>/tasks                    AGENT_TASKS_DIR (fixture task outputs)
#   <root>/bin/pgrep                stub reporting a running claude process
make_case() {
    local root="$TEST_TMPDIR/$1"
    mkdir -p "$root/home" "$root/messages/inbox" "$root/messages/config" \
             "$root/install" "$root/workspace" "$root/config" "$root/tasks" \
             "$root/bin" "$root/real"
    ln -s "$REPO_ROOT/scripts" "$root/install/scripts"
    sqlite3 "$root/messages/config/agent_sessions.db" \
        "CREATE TABLE agent_sessions (id TEXT, agent_type TEXT, status TEXT);"
    cat > "$root/bin/pgrep" <<'EOF'
#!/bin/bash
# Test stub: a Claude Code process is always "running".
for arg in "$@"; do
    if [ "$arg" = "-c" ]; then echo 2; exit 0; fi
done
exit 0
EOF
    chmod +x "$root/bin/pgrep"
    echo "$root"
}

# Add an agent_sessions row. Args: root id agent_type status
add_session_row() {
    sqlite3 "$1/messages/config/agent_sessions.db" \
        "INSERT INTO agent_sessions (id, agent_type, status) VALUES ('$2', '$3', '$4');"
}

# Add a finished (end_turn) task output. Real subagent outputs are symlinks
# to a JSONL transcript, so mirror that. Args: root task_id
add_end_turn_output() {
    local real="$1/real/$2.jsonl"
    echo '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"stop_reason":"end_turn"}}' > "$real"
    ln -s "$real" "$1/tasks/$2.output"
}

# Run the self-check script inside a case environment.
run_self_check() {
    local root="$1"
    env -i \
        PATH="$root/bin:/usr/local/bin:/usr/bin:/bin" \
        HOME="$root/home" \
        LOBSTER_MESSAGES="$root/messages" \
        LOBSTER_INSTALL_DIR="$root/install" \
        LOBSTER_WORKSPACE="$root/workspace" \
        LOBSTER_CONFIG_DIR="$root/config" \
        AGENT_TASKS_DIR="$root/tasks" \
        bash "$SELF_CHECK_SCRIPT" >/dev/null 2>&1
}

inbox_texts() {
    local f
    for f in "$1"/messages/inbox/*.json; do
        [ -f "$f" ] || continue
        jq -r '.text' "$f"
    done
}

inbox_count() {
    find "$1/messages/inbox" -maxdepth 1 -name '*.json' | wc -l | tr -d ' '
}

#===============================================================================
# Tests
#===============================================================================

echo -e "${BOLD}=== periodic-self-check.sh: no [Task Completed] duplicate ===${NC}"

begin_test "periodic-self-check.sh passes bash -n syntax check"
if bash -n "$SELF_CHECK_SCRIPT" 2>/dev/null; then pass; else fail "syntax error"; fi

begin_test "finished task + running subagent: status? message, no [Task Completed]"
ROOT=$(make_case running)
add_end_turn_output "$ROOT" "agent-fixture-01"
add_session_row "$ROOT" "agent-fixture-02" "general-purpose" "running"
run_self_check "$ROOT"
TEXTS=$(inbox_texts "$ROOT")
if echo "$TEXTS" | grep -q '\[Task Completed\]'; then
    fail "inbox contains a [Task Completed] message: '$TEXTS'"
elif [ "$(inbox_count "$ROOT")" != "1" ]; then
    fail "expected exactly 1 inbox message, got $(inbox_count "$ROOT"): '$TEXTS'"
elif ! echo "$TEXTS" | grep -q '^status? (Self-check)'; then
    fail "expected a 'status? (Self-check)' message, got: '$TEXTS'"
else
    pass
fi

begin_test "finished task + no running subagents: no inbox message at all"
ROOT=$(make_case idle)
add_end_turn_output "$ROOT" "agent-fixture-01"
add_session_row "$ROOT" "lobster-dispatcher" "dispatcher" "running"
run_self_check "$ROOT"
if [ "$(inbox_count "$ROOT")" = "0" ]; then
    pass
else
    fail "expected 0 inbox messages, got $(inbox_count "$ROOT"): '$(inbox_texts "$ROOT")'"
fi

begin_test "self-check does not record task ids in reported-tasks"
ROOT=$(make_case state)
add_end_turn_output "$ROOT" "agent-fixture-01"
add_session_row "$ROOT" "agent-fixture-02" "general-purpose" "running"
run_self_check "$ROOT"
if [ -s "$ROOT/install/.state/reported-tasks" ]; then
    fail "reported-tasks was written: '$(cat "$ROOT/install/.state/reported-tasks")'"
else
    pass
fi

echo ""
echo -e "${BOLD}Results: $PASS/$TOTAL passed${NC}"
if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
