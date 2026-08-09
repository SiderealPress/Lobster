#!/bin/bash
#===============================================================================
# Test Suite: update-lobster.sh no longer kills itself mid-update (issue #2179)
#
# Root cause: SERVICES_STOP used to list "lobster-claude" first. When
# update-lobster.sh runs as a descendant of lobster-claude.service's cgroup
# (the normal way it gets invoked -- via a Bash tool call from inside the
# live dispatcher session), stop_services()'s `systemctl stop lobster-claude`
# tears down that whole cgroup, including the running update-lobster.sh
# process itself, before git_update()/update_dependencies()/update_systemd()
# or the mcp-local/router restart ever run. systemd's Restart=on-failure then
# silently brings lobster-claude back up, which looks like "update finished"
# but nothing meaningful ever happened.
#
# Fix: lobster-claude is excluded from SERVICES_STOP/SERVICES_START entirely
# and is only ever bounced by restart_self_last(), called as the literal
# last statement of main() -- after git update, deps, systemd regen, CLI
# update, mcp-local/router restart, health checks, success notification, and
# lock cleanup have already completed.
#
# Part A: SERVICES_STOP/SERVICES_START/SERVICE_SELF constants -- pure check,
#         no process spawning.
# Part B: Full integration run of the real script, in a hermetic fake
#         environment, with systemctl/sudo/curl/claude mocked. The mock
#         systemctl reproduces the actual cgroup-kill dynamic: whenever it is
#         asked to touch "lobster-claude" (with self-kill simulation
#         enabled), it sends SIGTERM then SIGKILL to the running
#         update-lobster.sh process itself -- exactly what a real cgroup
#         teardown does. This proves the meaningful work (git update to the
#         target commit, mcp-local/router restart with new code, success
#         notification, lock cleanup) all completes BEFORE lobster-claude is
#         ever touched, so the update is not silently a no-op if that touch
#         happens to kill this process.
# Part C: Same hermetic run without self-kill simulation, to confirm the
#         reordering did not break the normal (non-self-hosted) update path.
#
# Usage: bash tests/test-update-lobster-self-restart-last.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATE_SCRIPT="$REPO_ROOT/scripts/update-lobster.sh"

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected '$expected', got '$actual'"; fi
}

assert_not_contains() {
    local haystack="$1" needle="$2"
    if [[ "$haystack" != *"$needle"* ]]; then pass; else fail "did not expect to find '$needle' in: $haystack"; fi
}

assert_contains() {
    local haystack="$1" needle="$2"
    if [[ "$haystack" == *"$needle"* ]]; then pass; else fail "expected to find '$needle' in: $haystack"; fi
}

assert_file_absent() {
    local path="$1"
    if [[ ! -f "$path" ]]; then pass; else fail "expected file to NOT exist: $path"; fi
}

echo "=== update-lobster.sh self-restart-last tests (issue #2179) ==="
echo ""

# ===================================================================
# Part A: SERVICES_STOP / SERVICES_START / SERVICE_SELF constants
# ===================================================================
echo "--- Part A: service ordering constants ---"

# Extract just the variable declarations (avoid sourcing the whole script,
# which unconditionally runs main() at the bottom).
eval "$(sed -n '/^SERVICE_SELF=/,/^SERVICES_START=/p' "$UPDATE_SCRIPT")"

begin_test "SERVICE_SELF is lobster-claude"
assert_eq "$SERVICE_SELF" "lobster-claude"

begin_test "SERVICES_STOP does not include lobster-claude"
assert_not_contains "${SERVICES_STOP[*]}" "lobster-claude"

begin_test "SERVICES_START does not include lobster-claude"
assert_not_contains "${SERVICES_START[*]}" "lobster-claude"

begin_test "SERVICES_STOP still includes lobster-mcp-local and lobster-router"
assert_contains "${SERVICES_STOP[*]}" "lobster-mcp-local"
begin_test "SERVICES_STOP includes lobster-router"
assert_contains "${SERVICES_STOP[*]}" "lobster-router"

echo ""

# ===================================================================
# Shared harness for Parts B and C: build a hermetic fake environment and
# run the real script against it with systemctl/sudo/curl/claude mocked.
# ===================================================================

# setup_fixture: builds a fresh fake origin+clone repo, config, and mock bin
# dir. Sets globals used by run_scenario / assertions.
setup_fixture() {
    FIXTURE_DIR=$(mktemp -d /tmp/lobster-update-selfrestart-test-XXXXXX)
    FAKE_HOME="$FIXTURE_DIR/home"
    FAKE_LOBSTER_DIR="$FIXTURE_DIR/lobster"
    FAKE_ORIGIN_DIR="$FIXTURE_DIR/origin"
    FAKE_WORKSPACE_DIR="$FIXTURE_DIR/workspace"
    FAKE_MESSAGES_DIR="$FIXTURE_DIR/messages"
    FAKE_CONFIG_DIR="$FIXTURE_DIR/lobster-config"
    MOCK_BIN="$FIXTURE_DIR/bin"
    STATE_DIR="$FIXTURE_DIR/svc-state"
    CALL_LOG="$FIXTURE_DIR/systemctl-calls.log"
    CURL_LOG="$FIXTURE_DIR/curl-calls.log"
    TARGET_PID_FILE="$FIXTURE_DIR/target.pid"
    LOCK_FILE="$FIXTURE_DIR/update.lock"
    OUTPUT_LOG="$FIXTURE_DIR/update-output.log"

    mkdir -p "$FAKE_HOME" "$FAKE_WORKSPACE_DIR" "$FAKE_MESSAGES_DIR/processed" \
             "$FAKE_CONFIG_DIR" "$MOCK_BIN" "$STATE_DIR"

    # --- fake origin repo: base commit, then one commit ahead (the "update") ---
    git init -q -b main "$FAKE_ORIGIN_DIR"
    git -C "$FAKE_ORIGIN_DIR" config user.email test@example.invalid
    git -C "$FAKE_ORIGIN_DIR" config user.name "Test"
    mkdir -p "$FAKE_ORIGIN_DIR/scripts/lib"
    # Minimal stub so update_systemd()'s `source scripts/lib/template.sh`
    # succeeds even though this fixture has no real service templates.
    cat > "$FAKE_ORIGIN_DIR/scripts/lib/template.sh" <<'STUB'
_tmpl_generate_from_template() { return 0; }
STUB
    echo "base" > "$FAKE_ORIGIN_DIR/VERSION"
    git -C "$FAKE_ORIGIN_DIR" add -A
    git -C "$FAKE_ORIGIN_DIR" commit -q -m "base commit"
    BASE_COMMIT=$(git -C "$FAKE_ORIGIN_DIR" rev-parse HEAD)

    echo "updated" > "$FAKE_ORIGIN_DIR/VERSION"
    git -C "$FAKE_ORIGIN_DIR" add -A
    git -C "$FAKE_ORIGIN_DIR" commit -q -m "the update the test is proving gets applied"
    NEW_COMMIT_SHORT=$(git -C "$FAKE_ORIGIN_DIR" rev-parse --short HEAD)

    # --- local clone, rolled back to the base commit (origin/main stays ahead) ---
    git clone -q "$FAKE_ORIGIN_DIR" "$FAKE_LOBSTER_DIR"
    git -C "$FAKE_LOBSTER_DIR" config user.email test@example.invalid
    git -C "$FAKE_LOBSTER_DIR" config user.name "Test"
    git -C "$FAKE_LOBSTER_DIR" reset -q --hard "$BASE_COMMIT"

    # --- config ---
    cat > "$FAKE_CONFIG_DIR/config.env" <<CFG
TELEGRAM_BOT_TOKEN=test-token-123
TELEGRAM_ALLOWED_USERS=999
CFG

    # --- initial simulated service state: all three "active" ---
    echo "active" > "$STATE_DIR/lobster-claude"
    echo "active" > "$STATE_DIR/lobster-mcp-local"
    echo "active" > "$STATE_DIR/lobster-router"

    : > "$CALL_LOG"
    : > "$CURL_LOG"

    build_mocks
}

build_mocks() {
    # --- mock systemctl: tracks simulated service state, logs every call,
    # and (when self-kill simulation is enabled) reproduces the real cgroup
    # teardown by sending SIGTERM/SIGKILL to the running update-lobster.sh
    # process the moment it is asked to touch lobster-claude. ---
    cat > "$MOCK_BIN/systemctl" <<'MOCK'
#!/bin/bash
STATE_DIR="${LOBSTER_TEST_SVC_STATE_DIR:?}"
LOG="${LOBSTER_TEST_SYSTEMCTL_LOG:-/dev/null}"
echo "$*" >> "$LOG"

action=""
service=""
for arg in "$@"; do
    case "$arg" in
        --no-block|--quiet) ;;
        is-active|stop|start|restart|kill|daemon-reload)
            [ -z "$action" ] && action="$arg" ;;
        *) [ -z "$service" ] && service="$arg" ;;
    esac
done

case "$action" in
    is-active)
        state=$(cat "$STATE_DIR/$service" 2>/dev/null || echo "inactive")
        [ "$state" = "active" ]
        exit $?
        ;;
    stop|kill)
        echo "inactive" > "$STATE_DIR/$service"
        ;;
    start|restart)
        echo "active" > "$STATE_DIR/$service"
        ;;
    daemon-reload)
        exit 0
        ;;
esac

# Reproduce the cgroup-teardown dynamic: touching lobster-claude while this
# script is (simulated to be) a descendant of its cgroup kills this process.
if [ "$service" = "lobster-claude" ] && [ "$action" != "is-active" ] \
   && [ "${LOBSTER_TEST_SIMULATE_SELF_KILL:-0}" = "1" ]; then
    pid_file="${LOBSTER_TEST_TARGET_PID_FILE:?}"
    if [ -f "$pid_file" ]; then
        target_pid=$(cat "$pid_file")
        kill -TERM "$target_pid" 2>/dev/null || true
        sleep 0.2
        kill -KILL "$target_pid" 2>/dev/null || true
    fi
fi

exit 0
MOCK
    chmod +x "$MOCK_BIN/systemctl"

    # --- mock sudo: logs, then delegates systemctl calls to the mock above
    # (everything else -- e.g. `sudo cp`, `sudo chmod` -- is executed for
    # real, but this fixture's fake repo has none of the files that would
    # trigger those paths, so nothing outside the fixture is ever touched). ---
    cat > "$MOCK_BIN/sudo" <<'MOCK'
#!/bin/bash
echo "$*" >> "${LOBSTER_TEST_SUDO_LOG:-/dev/null}"
if [ "$1" = "systemctl" ]; then
    shift
    exec "${MOCK_SYSTEMCTL:?}" "$@"
fi
exec "$@"
MOCK
    chmod +x "$MOCK_BIN/sudo"

    # --- mock claude: satisfies create_backup()'s `claude --version` and
    # health_checks()'s `claude mcp list`. ---
    cat > "$MOCK_BIN/claude" <<'MOCK'
#!/bin/bash
case "${1:-}" in
    mcp) echo "lobster-inbox  connected"; exit 0 ;;
    --version) echo "1.0.0-test"; exit 0 ;;
    *) exit 0 ;;
esac
MOCK
    chmod +x "$MOCK_BIN/claude"

    # --- mock curl: logs every call (so notify_success/notify_starting/
    # notify_failure POSTs can be asserted on) and fakes just enough
    # response content for preflight connectivity + telegram checks to pass,
    # without ever making a real network call. ---
    cat > "$MOCK_BIN/curl" <<'MOCK'
#!/bin/bash
echo "$*" >> "${LOBSTER_TEST_CURL_LOG:-/dev/null}"
url=""
for arg in "$@"; do
    case "$arg" in http*) url="$arg" ;; esac
done
case "$url" in
    *getMe*) echo '{"ok":true}' ;;
    *sendMessage*) echo '{"ok":true,"result":{}}' ;;
    *) : ;;
esac
exit 0
MOCK
    chmod +x "$MOCK_BIN/curl"
}

# run_scenario SIMULATE_SELF_KILL -- runs the real update-lobster.sh against
# the fixture built by setup_fixture, in the background, recording its PID
# where the mock systemctl can find it. Sets RUN_EXIT_CODE.
run_scenario() {
    local simulate_kill="$1"

    PATH="$MOCK_BIN:$PATH" \
    HOME="$FAKE_HOME" \
    LOBSTER_INSTALL_DIR="$FAKE_LOBSTER_DIR" \
    LOBSTER_WORKSPACE="$FAKE_WORKSPACE_DIR" \
    LOBSTER_MESSAGES="$FAKE_MESSAGES_DIR" \
    LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
    LOBSTER_UPDATE_LOCK_FILE="$LOCK_FILE" \
    MOCK_SYSTEMCTL="$MOCK_BIN/systemctl" \
    LOBSTER_TEST_SVC_STATE_DIR="$STATE_DIR" \
    LOBSTER_TEST_SYSTEMCTL_LOG="$CALL_LOG" \
    LOBSTER_TEST_CURL_LOG="$CURL_LOG" \
    LOBSTER_TEST_TARGET_PID_FILE="$TARGET_PID_FILE" \
    LOBSTER_TEST_SIMULATE_SELF_KILL="$simulate_kill" \
    bash "$UPDATE_SCRIPT" --skip-claude > "$OUTPUT_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$TARGET_PID_FILE"
    wait "$pid"
    RUN_EXIT_CODE=$?
}

cleanup_fixture() {
    [ -n "${FIXTURE_DIR:-}" ] && rm -rf "$FIXTURE_DIR"
}

# ===================================================================
# Part B: self-kill simulation -- the actual issue #2179 scenario
# ===================================================================
echo "--- Part B: reproduces the cgroup self-kill while touching lobster-claude ---"

setup_fixture
run_scenario 1

FINAL_HEAD=$(git -C "$FAKE_LOBSTER_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")

begin_test "git update completed before lobster-claude was ever touched"
assert_eq "$FINAL_HEAD" "$NEW_COMMIT_SHORT"

begin_test "lobster-mcp-local was restarted (with the new code) before lobster-claude was touched"
assert_contains "$(cat "$CALL_LOG")" "start lobster-mcp-local"

begin_test "lobster-router was restarted (with the new code) before lobster-claude was touched"
assert_contains "$(cat "$CALL_LOG")" "start lobster-router"

begin_test "lobster-claude was still eventually bounced (self-restart step actually ran)"
CLAUDE_CALL_LINE=$(grep -n "lobster-claude" "$CALL_LOG" | tail -1 | cut -d: -f1)
if [ -n "$CLAUDE_CALL_LINE" ]; then pass; else fail "no systemctl call ever touched lobster-claude"; fi

begin_test "lobster-claude was touched only AFTER mcp-local/router were restarted (self-last ordering)"
MCP_LOCAL_LINE=$(grep -n "start lobster-mcp-local" "$CALL_LOG" | head -1 | cut -d: -f1)
ROUTER_LINE=$(grep -n "start lobster-router" "$CALL_LOG" | head -1 | cut -d: -f1)
if [ -n "$CLAUDE_CALL_LINE" ] && [ -n "$MCP_LOCAL_LINE" ] && [ -n "$ROUTER_LINE" ] \
   && [ "$CLAUDE_CALL_LINE" -gt "$MCP_LOCAL_LINE" ] && [ "$CLAUDE_CALL_LINE" -gt "$ROUTER_LINE" ]; then
    pass
else
    fail "expected lobster-claude call (line $CLAUDE_CALL_LINE) after mcp-local (line $MCP_LOCAL_LINE) and router (line $ROUTER_LINE)"
fi

begin_test "success notification was sent before the self-kill point"
assert_contains "$(cat "$CURL_LOG")" "sendMessage"
begin_test "success notification text confirms the update, not a failure"
assert_contains "$(cat "$CURL_LOG")" "updated successfully"

begin_test "lock file was cleaned up even though the process was killed"
assert_file_absent "$LOCK_FILE"

cleanup_fixture

echo ""

# ===================================================================
# Part C: normal run (no self-kill) still completes cleanly end-to-end --
# regression guard that reordering did not break the happy path.
# ===================================================================
echo "--- Part C: normal run without self-kill still completes end-to-end ---"

setup_fixture
run_scenario 0

FINAL_HEAD_C=$(git -C "$FAKE_LOBSTER_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")

begin_test "exits 0 on a clean run"
assert_eq "$RUN_EXIT_CODE" "0"

begin_test "git update completed"
assert_eq "$FINAL_HEAD_C" "$NEW_COMMIT_SHORT"

begin_test "lobster-claude ends up restarted"
assert_eq "$(cat "$STATE_DIR/lobster-claude")" "active"

begin_test "output reports UPDATE COMPLETE"
assert_contains "$(cat "$OUTPUT_LOG")" "UPDATE COMPLETE"

cleanup_fixture

echo ""
echo "=== Results: $PASS/$TOTAL passed ==="
if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
