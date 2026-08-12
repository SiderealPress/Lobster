#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - reap_orphaned_mcp_processes() (issue #2119)
#
# Cron backstop for orphaned stdio MCP servers (obsidian-mcp specifically,
# pattern is configurable) that survive a session-age restart despite
# check_session_age()'s primary kill_dispatcher_children() fix -- e.g. a
# dispatcher crash, an OOM kill, or CC's 7440s hard session limit firing
# before the proactive restart gets a chance to signal children at all.
#
# Runs every health-check-v3.sh cron cycle (every 4 minutes). Only targets
# processes that are BOTH:
#   1. Reparented to init (PPID=1) -- actually orphaned, not a live MCP
#      child of the current dispatcher session
#   2. Older than the configured age threshold (production default 8100s =
#      2h15m; overridden much lower here via LOBSTER_ORPHAN_MCP_REAP_AGE_SECONDS
#      so the tests don't need to wait over two hours)
#
# Usage: bash tests/test-health-check-orphan-mcp-reaper.sh
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

TEST_TMPDIR=$(mktemp -d /tmp/lobster-orphan-reaper-test-XXXXXX)
spawned_pids=()
cleanup() {
    local p
    for p in "${spawned_pids[@]:-}"; do
        [[ -n "$p" ]] && kill -9 "$p" 2>/dev/null
    done
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

#===============================================================================
# Named constants (from the spec — never hardcode magic values in tests)
#===============================================================================
# A tiny threshold so the "old enough to reap" case doesn't require waiting
# over two hours in a test. Kept distinct from the production default
# (8100s) so it's obvious this is a test override, not the real value.
TEST_REAP_AGE_SECONDS=2
# A marker unlikely to ever collide with this test file's own invocation
# command line (avoids a pgrep -f self-match against the test runner).
FAKE_MARKER="reapfake${TEST_TMPDIR##*-}mcpstub"

#===============================================================================
# Stub the minimal environment reap_orphaned_mcp_processes() needs.
#===============================================================================
LOG_FILE="$TEST_TMPDIR/health-check.log"
log()       { echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

ORPHAN_MCP_REAP_PATTERN="$FAKE_MARKER"
ORPHAN_MCP_REAP_AGE_SECONDS="$TEST_REAP_AGE_SECONDS"

# Load reap_orphaned_mcp_processes() from the health check script.
eval "$(sed -n '/^reap_orphaned_mcp_processes()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

if ! declare -f reap_orphaned_mcp_processes > /dev/null 2>&1; then
    echo "FATAL: reap_orphaned_mcp_processes() not found in $HEALTH_SCRIPT"
    exit 1
fi

FAKE_BIN_DIR="$TEST_TMPDIR/bin"
mkdir -p "$FAKE_BIN_DIR"
FAKE_BIN="$FAKE_BIN_DIR/$FAKE_MARKER"
cat > "$FAKE_BIN" <<'EOF'
#!/bin/bash
sleep 60
EOF
chmod +x "$FAKE_BIN"

# NOTE: a "spawn a still-parented process" helper is deliberately NOT
# implemented as a function called via command substitution
# (pid=$(spawn_parented)) -- command substitution always runs in a subshell,
# and that subshell exits immediately after backgrounding the fake process,
# which orphans it right away (the same trick spawn_orphaned uses on
# purpose below). A genuinely still-parented process must be backgrounded
# directly in the top-level test script so its parent stays alive for the
# duration of the test. See the "parented_process_never_reaped" test below.

# Spawn a fake process and orphan it immediately (subshell backgrounds it
# then exits, so init/nearest subreaper adopts it -- PPID becomes 1).
spawn_orphaned() {
    local pidfile="$TEST_TMPDIR/orphan_pid_$$_$RANDOM"
    ( "$FAKE_BIN" >/dev/null 2>&1 & echo $! > "$pidfile" )
    # Wait for the pidfile to appear before reading it.
    for _ in $(seq 1 50); do
        [[ -s "$pidfile" ]] && break
        sleep 0.1
    done
    cat "$pidfile" 2>/dev/null
    rm -f "$pidfile"
}

echo ""
echo "=== Health Check Orphan MCP Reaper Tests (issue #2119) ==="
echo ""

# 1. A still-parented fake MCP process (PPID != 1) is left alone, regardless
# of age -- it's a legitimate child of a live session, not an orphan.
# Backgrounded directly in this top-level script (not via a function call
# through command substitution) so its parent is this script's own PID and
# stays alive for the duration of the test -- see the NOTE above.
begin_test "parented_process_never_reaped"
"$FAKE_BIN" >/dev/null 2>&1 &
pid=$!
spawned_pids+=("$pid")
ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
if [[ "$ppid" == "1" ]]; then
    fail "test setup failed: fake process was unexpectedly orphaned (PPID=1) immediately after backgrounding"
else
    sleep "$((TEST_REAP_AGE_SECONDS + 1))"
    reap_orphaned_mcp_processes
    if kill -0 "$pid" 2>/dev/null; then
        pass
    else
        fail "parented (non-orphan, PPID=$ppid) fake process $pid was killed — reaper must only ever touch PPID=1 processes"
    fi
fi
kill -9 "$pid" 2>/dev/null

# 2. An orphaned fake MCP process (PPID=1) younger than the age threshold is
# left alone -- avoids racing a process still mid-reparenting from a restart
# in progress right now.
begin_test "young_orphan_not_yet_reaped"
pid=$(spawn_orphaned)
if [[ -z "$pid" ]]; then
    fail "test setup failed: never observed the fake orphan PID"
else
    spawned_pids+=("$pid")
    ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [[ "$ppid" != "1" ]]; then
        fail "test setup failed: fake process PPID=$ppid, expected 1 (orphaning trick did not work in this environment)"
    else
        # Act immediately, well under TEST_REAP_AGE_SECONDS.
        reap_orphaned_mcp_processes
        if kill -0 "$pid" 2>/dev/null; then
            pass
        else
            fail "young orphan (age < ${TEST_REAP_AGE_SECONDS}s threshold) was reaped too early"
        fi
    fi
fi
kill -9 "$pid" 2>/dev/null || true

# 3. An orphaned fake MCP process (PPID=1) older than the age threshold IS
# reaped. This is the core positive case: it fails if reap_orphaned_mcp_processes()
# is removed or its age/PPID gating is broken.
begin_test "old_orphan_is_reaped"
pid=$(spawn_orphaned)
if [[ -z "$pid" ]]; then
    fail "test setup failed: never observed the fake orphan PID"
else
    spawned_pids+=("$pid")
    ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [[ "$ppid" != "1" ]]; then
        fail "test setup failed: fake process PPID=$ppid, expected 1"
    else
        sleep "$((TEST_REAP_AGE_SECONDS + 1))"
        reap_orphaned_mcp_processes
        sleep 1.5   # reaper's own SIGTERM -> 1s grace -> SIGKILL cycle
        if kill -0 "$pid" 2>/dev/null; then
            fail "old orphan PID $pid (age >= ${TEST_REAP_AGE_SECONDS}s, PPID=1) is still alive — reaper did not kill it"
        else
            pass
        fi
    fi
fi
kill -9 "$pid" 2>/dev/null || true

# 4. No matching processes at all -> no-op, no error.
begin_test "noop_when_no_candidates"
reap_orphaned_mcp_processes
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass
else
    fail "expected exit 0 with no candidate processes, got $rc"
fi

#===============================================================================
# Summary
#===============================================================================
echo ""
echo "Results: $PASS passed, $FAIL failed, $TOTAL total"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All orphan reaper tests passed.${NC}"
    exit 0
else
    echo -e "${RED}$FAIL test(s) failed.${NC}"
    exit 1
fi
