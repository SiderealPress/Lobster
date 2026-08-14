#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Session Age Check (issue #2059)
#
# Tests for check_session_age() — the proactive restart before the 7440s CC
# hard session lifetime limit.
#
# CC kills the dispatcher at exactly 7440s with no Stop hook. check_session_age()
# triggers a graceful SIGTERM at SESSION_AGE_LIMIT_SECONDS (7200s) so the
# Stop hook fires cleanly before the hard limit hits.
#
# Tests:
#   1. No start timestamp file → returns 0 (GREEN, no action)
#   2. Young session (age < SESSION_AGE_LIMIT_SECONDS) → returns 0 (GREEN)
#   3. Session at exact limit → sends SIGTERM, returns 1
#   4. Session past limit (age > SESSION_AGE_LIMIT_SECONDS) → sends SIGTERM, returns 1
#   5. SIGTERM sent to live dispatcher PID
#   6. No dispatcher.pid file → returns 0 (cannot act, skip gracefully)
#   7. Malformed start timestamp (non-integer) → returns 0 (graceful fallback)
#   8. Empty start timestamp file → returns 0 (graceful fallback)
#   9. Start file deleted after SIGTERM (prevents double-fire on next health check)
#  10. Boot grace suppression: caller suppresses check during boot grace period
#      (check_session_age itself does not implement boot grace — suppression is in main())
#
# Usage: bash tests/test-health-check-session-age.sh
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

TEST_TMPDIR=$(mktemp -d /tmp/lobster-session-age-test-XXXXXX)
TEST_LOG_DIR="$TEST_TMPDIR/logs"
TEST_DATA_DIR="$TEST_TMPDIR/data"
TEST_MESSAGES_DIR="$TEST_TMPDIR/messages"
TEST_CONFIG_DIR="$TEST_MESSAGES_DIR/config"

cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

mkdir -p "$TEST_LOG_DIR" "$TEST_DATA_DIR" "$TEST_CONFIG_DIR" "$TEST_TMPDIR/alert-dedup"

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_exit() {
    local actual="$1" expected="$2"
    if [[ "$actual" -eq "$expected" ]]; then pass; else fail "expected exit $expected, got $actual"; fi
}

#===============================================================================
# Named constants (from the spec — never hardcode magic values in tests)
#===============================================================================
SESSION_AGE_LIMIT_SECONDS=7200        # from health-check-v3.sh default
DISPATCHER_SESSION_START_FILENAME="dispatcher-session-start.ts"
DISPATCHER_PID_FILENAME="dispatcher.pid"

#===============================================================================
# Stub the minimal environment for check_session_age() to run
#===============================================================================

LOG_FILE="$TEST_LOG_DIR/health-check.log"

log()       { echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE" 2>/dev/null; }
log_info()  { log INFO "$1"; }
log_warn()  { log WARN "$1"; }
log_error() { log ERROR "$1"; }

# Stub send_telegram_alert_deduped: write to a file so we can assert it was called.
TELEGRAM_ALERTS_FILE="$TEST_TMPDIR/telegram-alerts.txt"
send_telegram_alert_deduped() {
    echo "ALERT[$1]: $2" >> "$TELEGRAM_ALERTS_FILE"
}

# Override env vars that check_session_age() reads.
DISPATCHER_SESSION_START_FILE="$TEST_DATA_DIR/$DISPATCHER_SESSION_START_FILENAME"
DISPATCHER_PID_FILE="$TEST_CONFIG_DIR/$DISPATCHER_PID_FILENAME"
ALERT_DEDUP_DIR="$TEST_TMPDIR/alert-dedup"

# Load check_session_age(), kill_dispatcher_children() (issue #2119), and
# get_descendant_pids() (issue #2221) from the health check script.
# check_session_age() calls both of the other two directly, so all three must
# be extracted together or the call fails with "command not found" inside the
# test (previously masked by `set -u` not being fatal for undefined commands —
# the return code was unaffected, but the child-killing behavior was silently
# never exercised).
# We extract just these three functions to avoid sourcing the entire ~2000-line file.
eval "$(sed -n '/^check_session_age()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
eval "$(sed -n '/^kill_dispatcher_children()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
eval "$(sed -n '/^get_descendant_pids()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

if ! declare -f check_session_age > /dev/null 2>&1; then
    echo "FATAL: check_session_age() not found in $HEALTH_SCRIPT"
    exit 1
fi

if ! declare -f kill_dispatcher_children > /dev/null 2>&1; then
    echo "FATAL: kill_dispatcher_children() not found in $HEALTH_SCRIPT"
    exit 1
fi

if ! declare -f get_descendant_pids > /dev/null 2>&1; then
    echo "FATAL: get_descendant_pids() not found in $HEALTH_SCRIPT"
    exit 1
fi

#===============================================================================
# Helper: reset test state between tests
#===============================================================================
reset_state() {
    rm -f "$DISPATCHER_SESSION_START_FILE" "$DISPATCHER_PID_FILE"
    rm -f "$TELEGRAM_ALERTS_FILE"
}

#===============================================================================
# Helper: build a fake THREE-HOP MCP descendant tree matching the REAL
# production topology (issue #2221). Confirmed live via
# `ps -o pid,ppid,pgid,sid,comm` against an actual running obsidian-mcp
# instance:
#
#   claude (dispatcher)
#    `- npm exec obsidian-mcp ...   (hop 1 -- dispatcher's DIRECT child;
#                                     the ONLY pid a one-hop `pgrep -P
#                                     $dispatcher_pid` -- the pre-#2221 code
#                                     -- ever reached)
#        `- sh -c obsidian-mcp ...  (hop 2 -- `npm exec`/`npx` forks, not
#                                     execs, into this wrapper)
#            `- node .../obsidian-mcp (hop 3 -- forked, not exec'd, from the
#                                     `sh -c` above; this is the real worker
#                                     process that actually hangs)
#
# The fake tree below uses plain shell/sleep processes instead of the real
# npm/sh/node binaries, but the PARENT/CHILD SHAPE is the real one: three
# hops below the dispatcher, not one. This is what test 15 (single-hop fake
# child) above did NOT exercise -- see issue #2221's review of that gap.
#===============================================================================
fake_hop3_worker() {
    # Hop 3: the real worker (`node .../obsidian-mcp`) -- a leaf process.
    # `exec` replaces this shell with `sleep` so hop 3 is exactly one
    # process, matching the real chain's depth (no extra hidden hop from
    # forking sleep as a child instead of exec'ing into it).
    exec sleep 600
}

fake_hop2_sh_wrapper() {
    # Hop 2: `sh -c obsidian-mcp ...` -- forks hop 3 as its child.
    local pid_dir="$1"
    fake_hop3_worker &
    echo "$!" > "$pid_dir/hop3.pid"
    wait
}

fake_hop1_npm_exec() {
    # Hop 1: `npm exec obsidian-mcp ...` -- the dispatcher's DIRECT child.
    local pid_dir="$1"
    fake_hop2_sh_wrapper "$pid_dir" &
    echo "$!" > "$pid_dir/hop2.pid"
    wait
}

fake_dispatcher_with_three_hop_mcp_tree() {
    local pid_dir="$1"
    fake_hop1_npm_exec "$pid_dir" &
    echo "$!" > "$pid_dir/hop1.pid"
    wait
}

# Spawns the fake dispatcher plus its 3-hop descendant chain in the
# background, waits for all three hop pid files to appear, and echoes the
# fake dispatcher's own PID (the one to write into DISPATCHER_PID_FILE).
#
# This function is called via command substitution ($(...)) by callers, which
# forks a subshell whose stdout is the pipe being captured. Without an
# explicit redirect, the backgrounded fake-tree processes inherit that same
# pipe fd; since the leaf (`sleep 600`) never exits on its own, the pipe's
# write end would stay open and $(...) would hang forever waiting for EOF
# instead of returning as soon as the subshell's own `echo` finishes. Routing
# the background job's stdio to /dev/null avoids that hang.
spawn_fake_three_hop_tree() {
    local pid_dir="$1"
    rm -f "$pid_dir"/hop1.pid "$pid_dir"/hop2.pid "$pid_dir"/hop3.pid
    bash -c "$(declare -f fake_hop3_worker fake_hop2_sh_wrapper fake_hop1_npm_exec fake_dispatcher_with_three_hop_mcp_tree); fake_dispatcher_with_three_hop_mcp_tree '$pid_dir'" > /dev/null 2>&1 < /dev/null &
    local dispatcher_pid=$!
    local waited=0
    while [[ ! -f "$pid_dir/hop3.pid" && $waited -lt 50 ]]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    echo "$dispatcher_pid"
}

#===============================================================================
# Tests
#===============================================================================

echo ""
echo "=== Health Check Session Age Tests ==="
echo ""

# 1. No start timestamp file → returns 0 (GREEN, no action)
begin_test "no_start_file_returns_green"
reset_state
check_session_age
assert_exit $? 0

# 2. Young session (age = 0s) → returns 0 (GREEN)
begin_test "young_session_returns_green"
reset_state
echo "$(date +%s)" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 3. Session just under the limit (SESSION_AGE_LIMIT_SECONDS - 1) → returns 0
begin_test "session_just_under_limit_returns_green"
reset_state
early_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS + 1 ))
echo "$early_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 4. Session at exact limit → sends SIGTERM, returns 1
# We use a dummy process to receive SIGTERM.
begin_test "session_at_exact_limit_sends_sigterm"
reset_state
# Start a background sleep process to receive the SIGTERM.
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
at_limit_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS ))
echo "$at_limit_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
kill "$target_pid" 2>/dev/null || true  # clean up if SIGTERM didn't kill it
assert_exit $rc 1

# 5. Session past limit → sends SIGTERM, returns 1
begin_test "session_past_limit_sends_sigterm"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_limit_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_limit_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
kill "$target_pid" 2>/dev/null || true
assert_exit $rc 1

# 6. No dispatcher.pid file → returns 0 (cannot send SIGTERM, skips gracefully)
begin_test "no_pid_file_returns_green"
reset_state
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
# No DISPATCHER_PID_FILE written
check_session_age
assert_exit $? 0

# 7. Malformed start timestamp (non-integer) → returns 0 (graceful fallback)
begin_test "malformed_start_timestamp_returns_green"
reset_state
echo "not-a-number" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 8. Empty start timestamp file → returns 0 (graceful fallback)
begin_test "empty_start_file_returns_green"
reset_state
# shellcheck disable=SC2188
> "$DISPATCHER_SESSION_START_FILE"  # empty file
check_session_age
assert_exit $? 0

# 9. Start file deleted after SIGTERM (prevents double-fire on next health check run)
begin_test "start_file_deleted_after_sigterm"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_limit_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_limit_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ ! -f "$DISPATCHER_SESSION_START_FILE" ]]; then
    pass
else
    fail "start file still present after SIGTERM — double-fire risk on next health check"
fi

# 10. Dead PID in dispatcher.pid → returns 0 (cannot send SIGTERM to dead process)
begin_test "dead_pid_returns_green"
reset_state
# Find a PID that is definitely not alive.
dead_pid=999997
while kill -0 "$dead_pid" 2>/dev/null; do
    dead_pid=$((dead_pid - 1))
done
echo "$dead_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
assert_exit $? 0

# 11. Telegram alert is sent when SIGTERM fires (LOBSTER_DEBUG=true)
begin_test "telegram_alert_sent_on_sigterm"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
LOBSTER_DEBUG=true check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ -f "$TELEGRAM_ALERTS_FILE" ]] && grep -q "proactive-session-restart" "$TELEGRAM_ALERTS_FILE"; then
    pass
else
    fail "no Telegram alert with key 'proactive-session-restart' found after SIGTERM"
fi

# 12. Notification text is user-friendly (issue #2075): plain language, not raw
# PID / session-age implementation detail ("Dispatcher PID N sent SIGTERM" /
# "before the 7440s CC hard limit").
begin_test "notification_text_is_user_friendly"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
LOBSTER_DEBUG=true check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ -f "$TELEGRAM_ALERTS_FILE" ]]; then
    alert_text=$(cat "$TELEGRAM_ALERTS_FILE")
    if echo "$alert_text" | grep -qiE "planned|graceful restart|back in" \
        && ! echo "$alert_text" | grep -qE "Dispatcher PID|7440s"; then
        pass
    else
        fail "notification text is not plain-language (or still leaks implementation detail): $alert_text"
    fi
else
    fail "no Telegram alert file found"
fi

# 13. Notification fires BEFORE SIGTERM (issue #2075): verify the source ordering
# structurally. bash is single-threaded within a function body; if
# send_telegram_alert_deduped appears before the executable 'kill -TERM' line in
# check_session_age(), the alert is guaranteed to fire before SIGTERM delivery —
# so a session that dies immediately on SIGTERM still got the message out.
begin_test "notification_fires_before_sigterm"
fn_body=$(sed -n '/^check_session_age()/,/^}/p' "$HEALTH_SCRIPT")
alert_line=$(echo "$fn_body" | grep -n "send_telegram_alert_deduped" | head -1 | cut -d: -f1)
kill_line=$(echo "$fn_body" | grep -n 'if kill -TERM' | head -1 | cut -d: -f1)
if [[ -z "$alert_line" || -z "$kill_line" ]]; then
    fail "could not locate send_telegram_alert_deduped or executable 'kill -TERM' in check_session_age() — alert_line='$alert_line' kill_line='$kill_line'"
elif [[ "$alert_line" -lt "$kill_line" ]]; then
    pass
else
    fail "send_telegram_alert_deduped (line $alert_line) is NOT before kill -TERM (line $kill_line) in check_session_age()"
fi

# 14. LOBSTER_DEBUG not set (or false) → alert still suppressed entirely (existing
# noise-reduction behavior from 2026-06-14 must be preserved by this fix — the
# ordering/wording fix must not turn this back on for non-debug instances).
begin_test "telegram_alert_suppressed_when_debug_false"
reset_state
sleep 600 &
target_pid=$!
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
LOBSTER_DEBUG=false check_session_age
kill "$target_pid" 2>/dev/null || true
if [[ ! -f "$TELEGRAM_ALERTS_FILE" ]]; then
    pass
else
    fail "Telegram alert was sent even though LOBSTER_DEBUG=false: $(cat "$TELEGRAM_ALERTS_FILE")"
fi

# 15. Session-age SIGTERM also kills a stdio MCP child of the dispatcher
# (issue #2119). This is the "spawn a fake dispatcher + child process, trigger
# the session-age kill path, show the child dies too" proof the issue asks
# for: `sleep 600` stands in for the dispatcher (`claude`), and a second
# `sleep 600` spawned as its child stands in for a hung stdio MCP server
# (obsidian-mcp). Before this fix, check_session_age() only ever signalled
# the dispatcher PID itself — the child was orphaned, never signalled, and
# leaked indefinitely (confirmed in production: 12 live orphans, one per
# session-age restart). This test fails if kill_dispatcher_children() is not
# called from check_session_age() (i.e. if the fix in health-check-v3.sh is
# reverted) because the child sleep process will still be alive after
# check_session_age() returns.
begin_test "session_age_sigterm_also_kills_dispatcher_child"
reset_state
# Fake dispatcher: a shell that spawns a child and then just sleeps, so the
# child is a real child process of $target_pid (not of this test script).
bash -c 'sleep 600 & child=$!; echo "$child" > "'"$TEST_TMPDIR"'/child.pid"; wait' &
target_pid=$!
# Wait for the child.pid file to appear (child has been spawned and is running).
for _ in $(seq 1 50); do
    [[ -f "$TEST_TMPDIR/child.pid" ]] && break
    sleep 0.1
done
child_pid=$(cat "$TEST_TMPDIR/child.pid" 2>/dev/null || echo "")
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
sleep 0.3
# Capture aliveness BEFORE any manual cleanup — cleanup must not itself kill
# the child, or the assertion below would pass unconditionally regardless of
# whether kill_dispatcher_children() actually did its job.
child_alive=false
if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    child_alive=true
fi
kill "$target_pid" "$child_pid" 2>/dev/null || true  # cleanup, captured above already
if [[ -z "$child_pid" ]]; then
    fail "test setup failed: never observed the fake MCP child PID"
elif [[ "$rc" -eq 1 && "$child_alive" == "false" ]]; then
    pass
else
    fail "expected exit 1 and child PID $child_pid dead; got rc=$rc, child_alive=$child_alive"
fi
rm -f "$TEST_TMPDIR/child.pid"

# 16. kill_dispatcher_children() is a no-op (returns 0, no error) when called
# with no PIDs at all — e.g. a dispatcher that never spawned any MCP
# children yet. It takes the child PID list as arguments (pre-enumerated by
# the caller, see the ordering note in health-check-v3.sh) rather than a
# parent PID to enumerate itself, so "no children" here means an empty
# argument list.
begin_test "kill_dispatcher_children_noop_when_no_children"
reset_state
kill_dispatcher_children
rc=$?
assert_exit "$rc" 0

# 17. Falsifiability check: with kill_dispatcher_children() replaced by a
# true no-op stub (simulating the pre-fix behavior where check_session_age()
# never touched the dispatcher's children at all), the fake MCP child must
# survive the session-age SIGTERM. This directly demonstrates the bug this
# fix closes, using the same harness as test 15.
begin_test "pre_fix_behavior_would_leak_the_child"
reset_state
# Shadow kill_dispatcher_children with a no-op to simulate the reverted state.
kill_dispatcher_children() { return 0; }
bash -c 'sleep 600 & child=$!; echo "$child" > "'"$TEST_TMPDIR"'/child.pid"; wait' &
target_pid=$!
for _ in $(seq 1 50); do
    [[ -f "$TEST_TMPDIR/child.pid" ]] && break
    sleep 0.1
done
child_pid=$(cat "$TEST_TMPDIR/child.pid" 2>/dev/null || echo "")
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
still_alive=false
if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    still_alive=true
fi
# Clean up real state now that the assertion has been captured.
kill "$target_pid" "$child_pid" 2>/dev/null || true
if [[ "$rc" -eq 1 && "$still_alive" == "true" ]]; then
    pass
else
    fail "expected the no-op stub to reproduce the leak (child survives); rc=$rc still_alive=$still_alive"
fi
rm -f "$TEST_TMPDIR/child.pid"
# Restore the real function extracted from health-check-v3.sh for any later tests.
eval "$(sed -n '/^kill_dispatcher_children()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

# 18. Session-age SIGTERM reaches ALL THREE hops of a real obsidian-mcp-shaped
# descendant tree (issue #2221). Test 15/17 above only ever exercised a
# one-hop fake child (`sleep 600` directly under the fake dispatcher), which
# is exactly the simplified topology the #2221 review flagged as insufficient
# -- it could never have caught a gap that only shows up two hops further
# down. This test builds the real `npm exec -> sh -c -> node` shape (see the
# fake_hop*/spawn_fake_three_hop_tree helpers above) and asserts every hop is
# dead after check_session_age() fires, not just the dispatcher's direct
# child.
begin_test "session_age_sigterm_reaches_all_three_mcp_tree_hops"
reset_state
target_pid=$(spawn_fake_three_hop_tree "$TEST_TMPDIR")
hop1_pid=$(cat "$TEST_TMPDIR/hop1.pid" 2>/dev/null || echo "")
hop2_pid=$(cat "$TEST_TMPDIR/hop2.pid" 2>/dev/null || echo "")
hop3_pid=$(cat "$TEST_TMPDIR/hop3.pid" 2>/dev/null || echo "")
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
sleep 0.3
# Capture aliveness BEFORE any manual cleanup, same discipline as test 15 --
# cleanup must not be what kills these processes, or the assertion would pass
# unconditionally regardless of whether the fix actually reached every hop.
hop1_alive=false; hop2_alive=false; hop3_alive=false
[[ -n "$hop1_pid" ]] && kill -0 "$hop1_pid" 2>/dev/null && hop1_alive=true
[[ -n "$hop2_pid" ]] && kill -0 "$hop2_pid" 2>/dev/null && hop2_alive=true
[[ -n "$hop3_pid" ]] && kill -0 "$hop3_pid" 2>/dev/null && hop3_alive=true
kill "$target_pid" "$hop1_pid" "$hop2_pid" "$hop3_pid" 2>/dev/null || true
if [[ -z "$hop1_pid" || -z "$hop2_pid" || -z "$hop3_pid" ]]; then
    fail "test setup failed: never observed all three fake MCP tree PIDs (hop1=$hop1_pid hop2=$hop2_pid hop3=$hop3_pid)"
elif [[ "$rc" -eq 1 && "$hop1_alive" == "false" && "$hop2_alive" == "false" && "$hop3_alive" == "false" ]]; then
    pass
else
    fail "expected exit 1 and all three hops dead; got rc=$rc hop1_alive=$hop1_alive hop2_alive=$hop2_alive hop3_alive=$hop3_alive"
fi
rm -f "$TEST_TMPDIR"/hop1.pid "$TEST_TMPDIR"/hop2.pid "$TEST_TMPDIR"/hop3.pid

# 19. Falsifiability check for #2221: with get_descendant_pids() shadowed by
# the exact pre-#2221 one-hop implementation (`pgrep -P $dispatcher_pid`,
# i.e. what check_session_age() called before this fix), hop 1 (the `npm
# exec` wrapper, a direct child of the dispatcher) dies as before, but hop 2
# and hop 3 -- the `sh -c` wrapper and the real `node` worker underneath it --
# must survive, reproducing the exact orphan leak #2221 reports. This
# directly demonstrates that test 18 above would have failed against the
# pre-fix code, and passes only because get_descendant_pids() now walks the
# full tree.
begin_test "pre_2221_fix_one_hop_pgrep_would_leak_hop2_and_hop3"
reset_state
# Shadow get_descendant_pids with the pre-#2221 one-hop implementation to
# simulate the reverted state.
get_descendant_pids() { pgrep -P "$1" 2>/dev/null || true; }
target_pid=$(spawn_fake_three_hop_tree "$TEST_TMPDIR")
hop1_pid=$(cat "$TEST_TMPDIR/hop1.pid" 2>/dev/null || echo "")
hop2_pid=$(cat "$TEST_TMPDIR/hop2.pid" 2>/dev/null || echo "")
hop3_pid=$(cat "$TEST_TMPDIR/hop3.pid" 2>/dev/null || echo "")
echo "$target_pid" > "$DISPATCHER_PID_FILE"
past_start=$(( $(date +%s) - SESSION_AGE_LIMIT_SECONDS - 60 ))
echo "$past_start" > "$DISPATCHER_SESSION_START_FILE"
check_session_age
rc=$?
sleep 0.3
hop1_alive=false; hop2_alive=false; hop3_alive=false
[[ -n "$hop1_pid" ]] && kill -0 "$hop1_pid" 2>/dev/null && hop1_alive=true
[[ -n "$hop2_pid" ]] && kill -0 "$hop2_pid" 2>/dev/null && hop2_alive=true
[[ -n "$hop3_pid" ]] && kill -0 "$hop3_pid" 2>/dev/null && hop3_alive=true
kill "$target_pid" "$hop1_pid" "$hop2_pid" "$hop3_pid" 2>/dev/null || true
if [[ "$rc" -eq 1 && "$hop1_alive" == "false" && "$hop2_alive" == "true" && "$hop3_alive" == "true" ]]; then
    pass
else
    fail "expected the pre-#2221 one-hop pgrep to reproduce the leak (hop1 dead, hop2+hop3 survive); rc=$rc hop1_alive=$hop1_alive hop2_alive=$hop2_alive hop3_alive=$hop3_alive"
fi
rm -f "$TEST_TMPDIR"/hop1.pid "$TEST_TMPDIR"/hop2.pid "$TEST_TMPDIR"/hop3.pid
# Restore the real get_descendant_pids() extracted from health-check-v3.sh for any later tests.
eval "$(sed -n '/^get_descendant_pids()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

#===============================================================================
# Summary
#===============================================================================
echo ""
echo "Results: $PASS passed, $FAIL failed, $TOTAL total"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All session age tests passed.${NC}"
    exit 0
else
    echo -e "${RED}$FAIL test(s) failed.${NC}"
    exit 1
fi
