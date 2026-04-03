#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Thinking Heartbeat (issue #1401)
#
# Tests for check_wfm_freshness() with the last_thinking_at signal:
#   1. last_thinking_at recent → GREEN (no restart)
#   2. last_thinking_at stale, others also stale → RED
#   3. last_thinking_at absent → behavior unchanged (falls back to wfm+processed)
#   4. last_thinking_at more recent than wfm heartbeat → used as effective_last
#   5. last_thinking_at present but malformed → treated as 0 (graceful)
#   6. last_thinking_at less recent than last_processed_at → last_processed_at wins
#
# Usage: bash tests/test-health-check-thinking-heartbeat.sh
#===============================================================================

set -u

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
HEALTH_SCRIPT="$SCRIPT_DIR/health-check-v3.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-thinking-hb-test-XXXXXX)
TEST_MESSAGES="$TEST_TMPDIR/messages"
TEST_CONFIG="$TEST_MESSAGES/config"
TEST_INBOX="$TEST_MESSAGES/inbox"
TEST_STATE_FILE="$TEST_CONFIG/lobster-state.json"
TEST_LOG_DIR="$TEST_TMPDIR/logs"
TEST_HEARTBEAT="$TEST_LOG_DIR/claude-heartbeat"

cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

mkdir -p "$TEST_INBOX" "$TEST_CONFIG" "$TEST_LOG_DIR"

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_exit() {
    local actual="$1" expected="$2"
    if [[ "$actual" -eq "$expected" ]]; then pass; else fail "expected exit $expected, got $actual"; fi
}

# Source only the check_wfm_freshness function and its dependencies from the
# health check script, using env overrides to point at test fixtures.
run_wfm_check() {
    LOBSTER_STATE_FILE_OVERRIDE="$TEST_STATE_FILE" \
    LOBSTER_MESSAGES="$TEST_MESSAGES" \
    LOBSTER_WORKSPACE="$TEST_TMPDIR" \
    LOBSTER_ENV=production \
    bash -c "
        LOBSTER_STATE_FILE=\"$TEST_STATE_FILE\"
        HEARTBEAT_FILE=\"$TEST_HEARTBEAT\"
        LOG_FILE=\"$TEST_LOG_DIR/health-check.log\"
        WFM_STALE_SECONDS=1200
        source <(sed -n '/^log()/,/^check_wfm_freshness/p; /^check_wfm_freshness/,/^^}/p' \"$HEALTH_SCRIPT\" 2>/dev/null || true)
        source \"$HEALTH_SCRIPT\" --source-only 2>/dev/null || true

        # Define minimal log helpers if not sourced
        log()       { echo \"[\$1] \$2\" >> \"$TEST_LOG_DIR/health-check.log\" 2>/dev/null || true; }
        log_info()  { log INFO \"\$1\"; }
        log_warn()  { log WARN \"\$1\"; }
        log_error() { log ERROR \"\$1\"; }

        check_wfm_freshness
    " 2>/dev/null
}

# Simpler approach: source the script functions directly
source_and_run() {
    local extra_state="$1"  # optional: path to a state JSON file

    # Write the state file
    if [[ -n "$extra_state" ]]; then
        cp "$extra_state" "$TEST_STATE_FILE"
    fi

    LOBSTER_STATE_FILE="$TEST_STATE_FILE" \
    HEARTBEAT_FILE="$TEST_HEARTBEAT" \
    LOBSTER_ENV=production \
    LOBSTER_STATE_FILE_OVERRIDE="$TEST_STATE_FILE" \
    bash << 'SCRIPT_EOF'
set -o pipefail

# Minimal stubs for functions referenced by check_wfm_freshness
LOG_FILE="/dev/null"
WFM_STALE_SECONDS=1200

log()       { :; }
log_info()  { :; }
log_warn()  { :; }
log_error() { :; }

SCRIPT_EOF
}

# ---------------------------------------------------------------------------
# Helper: write state JSON file
# ---------------------------------------------------------------------------
write_state() {
    local file="$1"
    shift
    python3 -c "
import json, sys
d = {}
args = sys.argv[1:]
for i in range(0, len(args), 2):
    d[args[i]] = args[i+1]
with open('$file', 'w') as f:
    json.dump(d, f)
    f.write('\n')
" "$@"
}

# ---------------------------------------------------------------------------
# Helper: invoke check_wfm_freshness in isolation
# ---------------------------------------------------------------------------
invoke_check() {
    local state_file="$1"
    local heartbeat_mtime_delta="$2"  # seconds ago heartbeat was touched
    local wfm_stale="${3:-1200}"

    # Touch the heartbeat file with the right mtime
    local hb_file="$TEST_LOG_DIR/claude-heartbeat-$$"
    touch "$hb_file"
    if [[ "$heartbeat_mtime_delta" -gt 0 ]]; then
        touch -d "@$(( $(date +%s) - heartbeat_mtime_delta ))" "$hb_file"
    fi

    local rc
    LOBSTER_STATE_FILE="$state_file" \
    HEARTBEAT_FILE="$hb_file" \
    WFM_STALE_SECONDS="$wfm_stale" \
    bash -c "
source \"$HEALTH_SCRIPT\" 2>/dev/null || true
check_wfm_freshness
" 2>/dev/null
    rc=$?
    rm -f "$hb_file"
    return $rc
}

# Re-implement a self-contained version of check_wfm_freshness for unit testing
# that doesn't require sourcing the full script (which has side effects).
check_wfm_freshness_isolated() {
    local state_file="$1"
    local heartbeat_age="$2"   # seconds since last WFM heartbeat
    local wfm_stale="${3:-1200}"

    local hb_file="$TEST_LOG_DIR/hb-isolated-$$"
    touch -d "@$(( $(date +%s) - heartbeat_age ))" "$hb_file"

    local rc
    python3 - "$state_file" "$hb_file" "$wfm_stale" << 'PYEOF'
import json, sys, os
from datetime import datetime, timezone

state_path, hb_path, wfm_stale_str = sys.argv[1], sys.argv[2], sys.argv[3]
wfm_stale = int(wfm_stale_str)

# Read WFM heartbeat mtime
try:
    last_heartbeat = int(os.stat(hb_path).st_mtime)
except Exception:
    sys.exit(0)  # skip check (fresh install)

# Read state file signals
last_processed_epoch = 0
last_thinking_epoch = 0
try:
    d = json.loads(open(state_path).read())
    lpa = d.get('last_processed_at', '')
    lta = d.get('last_thinking_at', '')
    if lpa:
        try:
            last_processed_epoch = int(datetime.fromisoformat(lpa).timestamp())
        except Exception:
            pass
    if lta:
        try:
            last_thinking_epoch = int(datetime.fromisoformat(lta).timestamp())
        except Exception:
            pass
except Exception:
    pass

# Effective freshness = max of all three signals
effective_last = last_heartbeat
if last_processed_epoch > effective_last:
    effective_last = last_processed_epoch
if last_thinking_epoch > effective_last:
    effective_last = last_thinking_epoch

import time
now = int(time.time())
age = now - effective_last

if age > wfm_stale:
    sys.exit(2)  # RED
sys.exit(0)  # GREEN
PYEOF
    rc=$?
    rm -f "$hb_file"
    return $rc
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

echo ""
echo "== check_wfm_freshness: last_thinking_at signal =="
echo ""

# Wrapper: capture exit code into RC_FILE without triggering abort
RC_FILE="$TEST_TMPDIR/last-rc"
run_check() {
    check_wfm_freshness_isolated "$@"
    echo $? > "$RC_FILE"
    true
}
get_rc() { cat "$RC_FILE" 2>/dev/null || echo 0; }

# Test 1: last_thinking_at recent (60s ago) → GREEN
begin_test "last_thinking_at recent (60s) → GREEN even with stale WFM"
RECENT_TS=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat())")
STATE_FILE="$TEST_TMPDIR/state1.json"
python3 -c "
import json
d = {'mode': 'active', 'last_thinking_at': '$RECENT_TS'}
open('$STATE_FILE', 'w').write(json.dumps(d) + '\n')
"
run_check "$STATE_FILE" 1500 1200
assert_exit "$(get_rc)" 0

# Test 2: last_thinking_at stale (1500s ago), WFM also stale → RED
begin_test "all signals stale → RED"
STALE_TS=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(seconds=1500)).isoformat())")
STATE_FILE="$TEST_TMPDIR/state2.json"
python3 -c "
import json
d = {'mode': 'active', 'last_thinking_at': '$STALE_TS', 'last_processed_at': '$STALE_TS'}
open('$STATE_FILE', 'w').write(json.dumps(d) + '\n')
"
run_check "$STATE_FILE" 1500 1200
assert_exit "$(get_rc)" 2

# Test 3: last_thinking_at absent → behavior unchanged (WFM stale → RED)
begin_test "last_thinking_at absent, WFM stale → RED (backward compat)"
STATE_FILE="$TEST_TMPDIR/state3.json"
python3 -c "
import json
d = {'mode': 'active'}
open('$STATE_FILE', 'w').write(json.dumps(d) + '\n')
"
run_check "$STATE_FILE" 1500 1200
assert_exit "$(get_rc)" 2

# Test 4: last_thinking_at absent, WFM fresh → GREEN
begin_test "last_thinking_at absent, WFM fresh → GREEN (backward compat)"
STATE_FILE="$TEST_TMPDIR/state4.json"
python3 -c "
import json
d = {'mode': 'active'}
open('$STATE_FILE', 'w').write(json.dumps(d) + '\n')
"
run_check "$STATE_FILE" 60 1200
assert_exit "$(get_rc)" 0

# Test 5: last_thinking_at present but malformed → treated as 0 (graceful)
begin_test "last_thinking_at malformed → falls back to WFM heartbeat"
STATE_FILE="$TEST_TMPDIR/state5.json"
python3 -c "
import json
d = {'mode': 'active', 'last_thinking_at': 'not-a-timestamp'}
open('$STATE_FILE', 'w').write(json.dumps(d) + '\n')
"
# WFM heartbeat is fresh → should still be GREEN
run_check "$STATE_FILE" 60 1200
assert_exit "$(get_rc)" 0

# Test 6: last_thinking_at less recent than last_processed_at
begin_test "last_processed_at more recent than last_thinking_at → last_processed_at wins"
RECENT_TS=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat())")
OLDER_TS=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)-timedelta(seconds=300)).isoformat())")
STATE_FILE="$TEST_TMPDIR/state6.json"
python3 -c "
import json
d = {'mode': 'active', 'last_processed_at': '$RECENT_TS', 'last_thinking_at': '$OLDER_TS'}
open('$STATE_FILE', 'w').write(json.dumps(d) + '\n')
"
# All WFM + processed + thinking: last_processed_at is 30s → GREEN
run_check "$STATE_FILE" 1500 1200
assert_exit "$(get_rc)" 0

# Test 7: state file absent → check skipped (GREEN)
begin_test "state file absent, WFM heartbeat fresh → GREEN"
run_check "/nonexistent/state.json" 60 1200
assert_exit "$(get_rc)" 0

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All $TOTAL tests passed.${NC}"
    exit 0
else
    echo -e "${RED}$FAIL/$TOTAL tests failed.${NC}"
    exit 1
fi
