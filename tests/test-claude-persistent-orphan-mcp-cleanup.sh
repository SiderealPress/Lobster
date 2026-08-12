#!/bin/bash
#===============================================================================
# Test Suite: claude-persistent.sh - kill_orphaned_mcp_processes() (issue #2119)
#
# Root cause under test: kill_orphaned_mcp_processes() is supposed to skip
# systemd-managed MCP services (e.g. the real MCP HTTP server) and kill
# everything else that looks orphaned. The guard it used, `systemctl status
# "$pid"`, does NOT ask "is $pid itself a systemd service" -- it resolves
# $pid's cgroup to whichever unit owns that cgroup and reports THAT unit's
# status with exit 0, for ANY pid living anywhere inside it. Every process
# under lobster-claude.service's tmux session (the dispatcher, every MCP
# child it spawns -- including a hung/orphaned obsidian-mcp process, which
# keeps its original cgroup membership even after being reparented to PID 1)
# resolves to lobster-claude.service and returns exit 0. So the old guard
# always treated every MCP child as "systemd-managed" and never killed it --
# confirmed against real production orphan PIDs during investigation of this
# issue (12 live orphaned obsidian-mcp processes, one per session-age
# restart, accumulating indefinitely because this guard silently protected
# every single one of them).
#
# The fix compares against the actual recorded MainPID of the protected
# systemd unit (`systemctl show -p MainPID --value <service>`) instead of
# the cgroup-membership test.
#
# This test suite runs INSIDE the real lobster-claude.service cgroup (this
# is itself a subagent process under that service), so `systemctl status
# $$` demonstrably returns exit 0 for this test's own PID -- the same
# condition that made the old guard falsely "protect" every MCP child. This
# is intentionally an integration-style test against the *real* systemctl
# and the *real* lobster-mcp.service, not a mock: it proves the fix holds
# under the actual production condition that caused the bug, not just a
# synthetic double. No systemd unit is started, stopped, or otherwise
# mutated -- only `systemctl status`/`show` (read-only) are used, and the
# only processes signalled are throwaway `sleep`-based fakes spawned by this
# test.
#
# Usage: bash tests/test-claude-persistent-orphan-mcp-cleanup.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
CLAUDE_PERSISTENT_SCRIPT="$SCRIPT_DIR/claude-persistent.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-orphan-mcp-test-XXXXXX)
cleanup() {
    [[ -n "${fake_pid:-}" ]] && kill -9 "$fake_pid" 2>/dev/null
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

# The pattern string is built from parts so this test file's own invocation
# (`bash tests/test-claude-persistent-orphan-mcp-cleanup.sh ...`) never
# contains the literal fake-binary marker string as a substring of its own
# command line -- avoiding a pgrep -f self-match against the test runner.
FAKE_MARKER="fake${TEST_TMPDIR##*-}obsidian_stub"

#===============================================================================
# Stub the minimal environment kill_orphaned_mcp_processes() needs.
#===============================================================================
LOG_FILE="$TEST_TMPDIR/claude-persistent.log"
log() {
    local msg="[$(date -Iseconds)] $1"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null
}

# Build a fake "obsidian-mcp"-shaped process whose command line contains
# FAKE_MARKER instead of the real name, and patch the pgrep pattern the
# extracted function uses to search for FAKE_MARKER instead of the literal
# string "obsidian-mcp" -- this keeps the test fully isolated from any real
# obsidian-mcp process that might legitimately be running on this host right
# now (there are live ones on this production box; the test must not
# interact with them in either direction).
FAKE_BIN_DIR="$TEST_TMPDIR/bin"
mkdir -p "$FAKE_BIN_DIR"
FAKE_BIN="$FAKE_BIN_DIR/$FAKE_MARKER"
cat > "$FAKE_BIN" <<'EOF'
#!/bin/bash
sleep 600
EOF
chmod +x "$FAKE_BIN"

# Extract kill_orphaned_mcp_processes() from claude-persistent.sh, then:
#  1. retarget its process-pattern search at FAKE_MARKER instead of the two
#     hardcoded real patterns (inbox_server.py / obsidian-mcp), so this test
#     only ever sees and signals its own fake process tree.
#  2. neutralize the tmux-pane ownership check (force tmux_panes empty).
#     This test's own process tree is a real descendant of the live
#     production tmux session (this test runs inside a Task-spawned
#     subagent, itself a descendant of the dispatcher's tmux pane) -- so
#     without this, the ancestor-walk would correctly (by design) classify
#     the fake process as a "current-session descendant" and skip it,
#     which is a DIFFERENT code path than the systemctl guard this test
#     targets. Forcing tmux_panes empty isolates the systemctl-guard fix
#     under test from the (working, not-under-test) tmux ownership check.
fn_body=$(sed -n '/^kill_orphaned_mcp_processes()/,/^}/p' "$CLAUDE_PERSISTENT_SCRIPT")
if [[ -z "$fn_body" ]]; then
    echo "FATAL: kill_orphaned_mcp_processes() not found in $CLAUDE_PERSISTENT_SCRIPT"
    exit 1
fi
fn_body=$(echo "$fn_body" | sed \
    -e 's/pgrep -f "src\/mcp\/inbox_server\\.py"/true/' \
    -e "s/pgrep -f \"obsidian-mcp\"/pgrep -f \"$FAKE_MARKER\"/" \
    -e "s/tmux_panes=\$(tmux -L lobster list-panes -a -F '#{pane_pid}' 2>\/dev\/null || true)/tmux_panes=\"\"/")
eval "$fn_body"

if ! declare -f kill_orphaned_mcp_processes | grep -q 'tmux_panes=""'; then
    echo "FATAL: tmux_panes neutralization sed did not match -- refusing to run (would test against the live tmux session instead of an isolated fake)"
    exit 1
fi

if ! declare -f kill_orphaned_mcp_processes > /dev/null 2>&1; then
    echo "FATAL: kill_orphaned_mcp_processes() did not load"
    exit 1
fi

echo ""
echo "=== claude-persistent.sh: kill_orphaned_mcp_processes() Tests (issue #2119) ==="
echo ""

# Sanity: confirm this test process really does run inside a systemd-unit
# cgroup, i.e. the exact condition that caused the original bug. If this
# ever fails (e.g. running outside the Lobster production host), the rest
# of this suite is still valid but is no longer proving the live-condition
# claim in the header comment -- flag it rather than silently passing.
begin_test "sanity_this_process_is_inside_a_systemd_cgroup"
if systemctl status $$ >/dev/null 2>&1; then
    pass
else
    fail "systemctl status \$\$ returned nonzero -- not running inside a systemd cgroup; the cgroup-membership bug this test targets cannot be demonstrated in this environment"
fi

# 1. A fake orphaned MCP process is killed by the FIXED guard, even though
# `systemctl status` on its own PID returns exit 0 (same cgroup as this
# test process -- the exact condition that made the OLD guard skip it).
begin_test "fixed_guard_kills_orphan_despite_systemctl_status_returning_zero"
"$FAKE_BIN" &
fake_pid=$!
sleep 0.3
# Confirm the precondition: systemctl status on the fake PID returns 0,
# reproducing the exact bug condition (cgroup-membership false positive).
if ! systemctl status "$fake_pid" >/dev/null 2>&1; then
    fail "precondition failed: systemctl status $fake_pid did not return 0 -- cannot demonstrate the bug in this environment"
else
    kill_orphaned_mcp_processes
    sleep 3.5   # kill_orphaned_mcp_processes' own SIGTERM->3s grace->SIGKILL cycle
    if kill -0 "$fake_pid" 2>/dev/null; then
        fail "fake orphan PID $fake_pid is still alive -- the fixed guard did not kill it"
        kill -9 "$fake_pid" 2>/dev/null
    else
        pass
    fi
fi
unset fake_pid

# 2. The real, unrelated lobster-mcp.service is never touched by this
# function (its MainPID must still be alive and must not appear in any
# SIGTERM/SIGKILL branch reachable by the fake-marker-only pattern used in
# test 1). This is a read-only assertion -- we do not invoke
# kill_orphaned_mcp_processes() with the real "obsidian-mcp"/"inbox_server.py"
# patterns in this test suite (those live processes on this production host
# are out of scope for testing; the hard constraint is not to touch them).
begin_test "real_mcp_service_pid_still_alive_after_test"
real_main_pid=$(systemctl show -p MainPID --value lobster-mcp.service 2>/dev/null || true)
if [[ -n "$real_main_pid" && "$real_main_pid" != "0" ]] && kill -0 "$real_main_pid" 2>/dev/null; then
    pass
else
    fail "lobster-mcp.service MainPID ($real_main_pid) is not alive -- this test suite must never affect it"
fi

#===============================================================================
# Summary
#===============================================================================
echo ""
echo "Results: $PASS passed, $FAIL failed, $TOTAL total"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All orphaned-MCP cleanup tests passed.${NC}"
    exit 0
else
    echo -e "${RED}$FAIL test(s) failed.${NC}"
    exit 1
fi
