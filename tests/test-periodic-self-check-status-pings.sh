#!/bin/bash
#===============================================================================
# Test Suite: periodic-self-check.sh LOBSTER_SELF_CHECK_STATUS_PINGS flag
#
# The self-check's "status?" message wakes the dispatcher every few minutes for
# the whole life of every running subagent. LOBSTER_SELF_CHECK_STATUS_PINGS in
# config.env (default true) lets an install turn that ping off; completion is
# still delivered by write_result. Only the literal values false/0 disable it.
#
# Same synthetic environment as test-periodic-self-check-no-task-completed.sh:
# temp HOME / LOBSTER_MESSAGES / LOBSTER_INSTALL_DIR / LOBSTER_WORKSPACE /
# LOBSTER_CONFIG_DIR, fixture agent_sessions.db with one running subagent row,
# PATH-stubbed `pgrep` so Guard 1 passes.
#
# Cases covered:
#   1. Script passes `bash -n`
#   2. LOBSTER_SELF_CHECK_STATUS_PINGS=false -> no inbox message
#   3. LOBSTER_SELF_CHECK_STATUS_PINGS=0     -> no inbox message
#   4. No config.env at all (default)        -> status? message written
#   5. config.env without the key (default)  -> status? message written
#   6. LOBSTER_SELF_CHECK_STATUS_PINGS=true  -> status? message written
#   7. Pings off does not advance the rate-limit timestamp
#
# Usage: bash tests/test-periodic-self-check-status-pings.sh
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

TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-self-check-pings-XXXXXX)
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

# Args: case-name config-line ("" = no config.env, "-" = config.env without key)
setup_ping_case() {
    local root
    root=$(make_case "$1")
    add_session_row "$root" "lobster-dispatcher" "dispatcher" "running"
    add_session_row "$root" "agent-fixture-02" "general-purpose" "running"
    case "$2" in
        "") ;;
        "-") echo "LOBSTER_DEV_MODE=false" > "$root/config/config.env" ;;
        *) printf 'LOBSTER_DEV_MODE=false\n%s\n' "$2" > "$root/config/config.env" ;;
    esac
    echo "$root"
}

expect_no_message() {
    local root="$1"
    run_self_check "$root"
    if [ "$(inbox_count "$root")" = "0" ]; then
        pass
    else
        fail "expected 0 inbox messages, got $(inbox_count "$root"): '$(inbox_texts "$root")'"
    fi
}

expect_status_message() {
    local root="$1"
    run_self_check "$root"
    local texts
    texts=$(inbox_texts "$root")
    if [ "$(inbox_count "$root")" != "1" ]; then
        fail "expected 1 inbox message, got $(inbox_count "$root"): '$texts'"
    elif ! echo "$texts" | grep -q '^status? (Self-check).*\[1 agents pending\]$'; then
        fail "expected a status? message for 1 pending agent, got: '$texts'"
    else
        pass
    fi
}

echo -e "${BOLD}=== periodic-self-check.sh: LOBSTER_SELF_CHECK_STATUS_PINGS ===${NC}"

begin_test "periodic-self-check.sh passes bash -n syntax check"
if bash -n "$SELF_CHECK_SCRIPT" 2>/dev/null; then pass; else fail "syntax error"; fi

begin_test "pings=false + running subagent: no status? message"
expect_no_message "$(setup_ping_case off-false "LOBSTER_SELF_CHECK_STATUS_PINGS=false")"

begin_test "pings=0 + running subagent: no status? message"
expect_no_message "$(setup_ping_case off-zero "LOBSTER_SELF_CHECK_STATUS_PINGS=0")"

begin_test "no config.env (default on): status? message written"
expect_status_message "$(setup_ping_case default-noconfig "")"

begin_test "config.env without the key (default on): status? message written"
expect_status_message "$(setup_ping_case default-nokey "-")"

begin_test "pings=true + running subagent: status? message written"
expect_status_message "$(setup_ping_case on-true "LOBSTER_SELF_CHECK_STATUS_PINGS=true")"

begin_test "pings=false does not write the rate-limit timestamp"
ROOT=$(setup_ping_case off-ratelimit "LOBSTER_SELF_CHECK_STATUS_PINGS=false")
run_self_check "$ROOT"
if [ -e "$ROOT/install/.state/last-self-check" ]; then
    fail "last-self-check was written although no message was sent"
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
