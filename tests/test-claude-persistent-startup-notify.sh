#!/bin/bash
#===============================================================================
# Test Suite: claude-persistent.sh - maybe_send_startup_alert() (issue #2159)
#
# Defect class under test: the "Lobster Starting" Telegram alert is sent on
# every lobster-claude.service start, including each iteration of a crash or
# restart loop, and there was no config key that could silence it
# (LOBSTER_DEBUG does not touch this path).
#
# The fix gates the alert on LOBSTER_STARTUP_NOTIFY (default true, so the
# existing behavior is preserved). When the key is "false" the alert is not
# sent and a single suppression line is logged instead.
#
# The script is sourced in library mode (its BASH_SOURCE guard skips main()),
# and both send_telegram_alert and log are replaced with recording stubs, so
# no network call is made and no real log file is written.
#
# Usage: bash tests/test-claude-persistent-startup-notify.sh
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

SUPPRESSION_LOG_LINE="Startup Telegram alert suppressed (LOBSTER_STARTUP_NOTIFY=false)"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-startup-notify-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

#===============================================================================
# Source the script in library mode with all runtime paths redirected to a
# temp dir (the script's top level runs mkdir -p on its state/log dirs).
#===============================================================================
export HOME="$TEST_TMPDIR/home"
export LOBSTER_WORKSPACE="$TEST_TMPDIR/workspace"
export LOBSTER_MESSAGES="$TEST_TMPDIR/messages"
export LOBSTER_CONFIG_DIR="$TEST_TMPDIR/config"
mkdir -p "$HOME"

# shellcheck source=/dev/null
source "$CLAUDE_PERSISTENT_SCRIPT"
# The sourced script installs its own signal traps and `set -uo pipefail`;
# restore our EXIT cleanup trap.
trap cleanup EXIT

#===============================================================================
# Recording stubs (defined after sourcing so they override the real ones).
#===============================================================================
ALERT_CALLS=0
SUPPRESS_LOG_CALLS=0
send_telegram_alert() { ALERT_CALLS=$((ALERT_CALLS + 1)); }
log() {
    if [[ "$1" == "$SUPPRESSION_LOG_LINE" ]]; then
        SUPPRESS_LOG_CALLS=$((SUPPRESS_LOG_CALLS + 1))
    fi
}

reset_counters() { ALERT_CALLS=0; SUPPRESS_LOG_CALLS=0; }

# Run the gate in the current shell (so the counters update) with the given
# LOBSTER_STARTUP_NOTIFY state: "__unset__" means the key is absent.
run_gate() {
    local value="$1"
    reset_counters
    if [[ "$value" == "__unset__" ]]; then
        unset LOBSTER_STARTUP_NOTIFY
    else
        LOBSTER_STARTUP_NOTIFY="$value"
    fi
    if ! declare -F maybe_send_startup_alert >/dev/null; then
        return 127
    fi
    maybe_send_startup_alert
}

echo "claude-persistent.sh: LOBSTER_STARTUP_NOTIFY gate"

#-------------------------------------------------------------------------------
begin_test "gate function exists in library mode"
if declare -F maybe_send_startup_alert >/dev/null; then
    pass
else
    fail "maybe_send_startup_alert is not defined"
fi

#-------------------------------------------------------------------------------
begin_test "LOBSTER_STARTUP_NOTIFY=false suppresses the alert and logs once"
run_gate "false"
rc=$?
if [[ $rc -eq 127 ]]; then
    fail "maybe_send_startup_alert is not defined"
elif [[ $ALERT_CALLS -eq 0 && $SUPPRESS_LOG_CALLS -eq 1 ]]; then
    pass
else
    fail "expected 0 alerts / 1 suppression log, got $ALERT_CALLS / $SUPPRESS_LOG_CALLS"
fi

#-------------------------------------------------------------------------------
begin_test "LOBSTER_STARTUP_NOTIFY unset sends the alert (default preserves behavior)"
run_gate "__unset__"
rc=$?
if [[ $rc -eq 127 ]]; then
    fail "maybe_send_startup_alert is not defined"
elif [[ $ALERT_CALLS -eq 1 && $SUPPRESS_LOG_CALLS -eq 0 ]]; then
    pass
else
    fail "expected 1 alert / 0 suppression log, got $ALERT_CALLS / $SUPPRESS_LOG_CALLS"
fi

#-------------------------------------------------------------------------------
begin_test "LOBSTER_STARTUP_NOTIFY=true sends the alert"
run_gate "true"
rc=$?
if [[ $rc -eq 127 ]]; then
    fail "maybe_send_startup_alert is not defined"
elif [[ $ALERT_CALLS -eq 1 && $SUPPRESS_LOG_CALLS -eq 0 ]]; then
    pass
else
    fail "expected 1 alert / 0 suppression log, got $ALERT_CALLS / $SUPPRESS_LOG_CALLS"
fi

#-------------------------------------------------------------------------------
begin_test "main() routes the startup alert through the gate"
# Guards against the gate function existing but main() still calling
# send_telegram_alert for "Lobster Starting" directly.
main_body=$(declare -f main)
if grep -q "maybe_send_startup_alert" <<<"$main_body" \
   && ! grep -q "Lobster Starting" <<<"$main_body"; then
    pass
else
    fail "main() does not call maybe_send_startup_alert (or still sends the alert inline)"
fi

echo
echo "Results: $PASS/$TOTAL passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
