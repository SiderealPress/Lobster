#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Boot Grace Period
#
# Tests for:
#   1. is_boot_grace_period() returns true when booted_at is recent
#   2. is_boot_grace_period() returns false when booted_at is old
#   3. is_boot_grace_period() returns false when booted_at is missing
#   4. write_boot_timestamp() merges booted_at without clobbering other fields
#   5. write_boot_stamp() in claude-persistent.sh writes booted_at
#
# Usage: bash tests/test-health-check-boot-grace.sh
#===============================================================================

set -eE

# Helper to capture exit code without triggering set -e
run_and_capture_rc() {
    "$@" && RC=$? || RC=$?
}

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# Counters
PASS=0
FAIL=0
TOTAL=0

# Script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
HEALTH_SCRIPT="$SCRIPT_DIR/health-check-v3.sh"
PERSISTENT_SCRIPT="$SCRIPT_DIR/claude-persistent.sh"

# Test isolation
TEST_TMPDIR=$(mktemp -d /tmp/lobster-boot-grace-test-XXXXXX)
TEST_MESSAGES="$TEST_TMPDIR/messages"
TEST_INBOX="$TEST_MESSAGES/inbox"
TEST_CONFIG="$TEST_MESSAGES/config"
TEST_STATE_FILE="$TEST_CONFIG/lobster-state.json"
TEST_LOG_DIR="$TEST_TMPDIR/logs"
TEST_LOG="$TEST_LOG_DIR/health-check.log"
TEST_RESTART_STATE="$TEST_LOG_DIR/health-restart-state-v3"
TEST_HEARTBEAT="$TEST_LOG_DIR/claude-heartbeat"

cleanup() {
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

mkdir -p "$TEST_INBOX" "$TEST_CONFIG" "$TEST_LOG_DIR"

#===============================================================================
# Helpers
#===============================================================================

begin_test() {
    TOTAL=$((TOTAL + 1))
    test_name="$1"
}

pass() {
    PASS=$((PASS + 1))
    echo -e "  ${GREEN}PASS${NC} $test_name"
}

fail() {
    FAIL=$((FAIL + 1))
    echo -e "  ${RED}FAIL${NC} $test_name: $1"
}

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then
        pass
    else
        fail "expected '$expected', got '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2"
    if echo "$haystack" | grep -q "$needle"; then
        pass
    else
        fail "expected log to contain '$needle', got:\n$haystack"
    fi
}

# Source the functions under test from health-check-v3.sh by extracting and
# evaluating them. We set the relevant env vars first so the functions work.
source_health_check_functions() {
    # Inject our test paths via env vars
    export LOBSTER_MESSAGES="$TEST_MESSAGES"
    export LOBSTER_WORKSPACE="$TEST_TMPDIR"
    export LOBSTER_STATE_FILE_OVERRIDE="$TEST_STATE_FILE"
    export LOBSTER_HEALTH_LOCK="$TEST_TMPDIR/health.lock"

    # Set the configuration constants needed by the extracted functions.
    # Assign directly from test paths rather than re-evaluating the config block,
    # so that overrides are always in effect regardless of script structure.
    BOOT_GRACE_SECONDS=90
    COMPACTION_SUPPRESS_SECONDS=300
    LOBSTER_STATE_FILE="${LOBSTER_STATE_FILE_OVERRIDE:-$TEST_STATE_FILE}"
    LOG_FILE="$TEST_LOG"

    # Extract specific named functions rather than everything up to main(), so
    # the sourcing does not break if main() is renamed or the file is restructured.
    eval "$(sed -n '/^log()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
    log_info()  { log "INFO"  "$1"; }
    log_warn()  { log "WARN"  "$1"; }
    log_error() { log "ERROR" "$1"; }
    eval "$(sed -n '/^is_boot_grace_period()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
    eval "$(sed -n '/^write_boot_timestamp()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
}

source_health_check_functions

#===============================================================================
# Test 1: is_boot_grace_period() — recent boot (within grace window)
#===============================================================================
begin_test "is_boot_grace_period: returns true for recent booted_at"

recent_ts=$(date -Iseconds)
cat > "$TEST_STATE_FILE" << EOF
{
  "mode": "active",
  "detail": "attempt=1",
  "updated_at": "$recent_ts",
  "pid": 12345,
  "booted_at": "$recent_ts"
}
EOF

run_and_capture_rc is_boot_grace_period
if [[ $RC -eq 0 ]]; then
    pass
else
    fail "expected 0 (grace window active), got $RC"
fi

#===============================================================================
# Test 2: is_boot_grace_period() — old boot (outside grace window)
#===============================================================================
begin_test "is_boot_grace_period: returns false for old booted_at"

old_ts=$(date -Iseconds -d "200 seconds ago")
cat > "$TEST_STATE_FILE" << EOF
{
  "mode": "active",
  "detail": "attempt=1",
  "updated_at": "$old_ts",
  "pid": 12345,
  "booted_at": "$old_ts"
}
EOF

run_and_capture_rc is_boot_grace_period
if [[ $RC -ne 0 ]]; then
    pass
else
    fail "expected non-zero (grace window expired), got $RC"
fi

#===============================================================================
# Test 3: is_boot_grace_period() — no booted_at field
#===============================================================================
begin_test "is_boot_grace_period: returns false when booted_at absent"

cat > "$TEST_STATE_FILE" << EOF
{
  "mode": "active",
  "detail": "attempt=1",
  "updated_at": "$(date -Iseconds)",
  "pid": 12345
}
EOF

run_and_capture_rc is_boot_grace_period
if [[ $RC -ne 0 ]]; then
    pass
else
    fail "expected non-zero (no booted_at field), got $RC"
fi

#===============================================================================
# Test 4: is_boot_grace_period() — state file absent
#===============================================================================
begin_test "is_boot_grace_period: returns false when state file missing"

rm -f "$TEST_STATE_FILE"

run_and_capture_rc is_boot_grace_period
if [[ $RC -ne 0 ]]; then
    pass
else
    fail "expected non-zero (no state file), got $RC"
fi

#===============================================================================
# Test 5: write_boot_timestamp() — merges booted_at without clobbering fields
#===============================================================================
begin_test "write_boot_timestamp: merges booted_at into existing state file"

cat > "$TEST_STATE_FILE" << EOF
{
  "mode": "active",
  "detail": "attempt=1",
  "updated_at": "$(date -Iseconds)",
  "pid": 99999,
  "compacted_at": "2026-01-01T00:00:00Z"
}
EOF

write_boot_timestamp

# Verify booted_at was added
booted_at_val=$(python3 -c "import json; d=json.load(open('$TEST_STATE_FILE')); print(d.get('booted_at','MISSING'))")
if [[ "$booted_at_val" == "MISSING" ]]; then
    fail "booted_at not written to state file"
else
    pass
fi

# Verify other fields were preserved
begin_test "write_boot_timestamp: preserves existing fields (pid, compacted_at)"
pid_val=$(python3 -c "import json; d=json.load(open('$TEST_STATE_FILE')); print(d.get('pid',0))")
compacted_val=$(python3 -c "import json; d=json.load(open('$TEST_STATE_FILE')); print(d.get('compacted_at','MISSING'))")
if [[ "$pid_val" == "99999" && "$compacted_val" != "MISSING" ]]; then
    pass
else
    fail "fields not preserved: pid=$pid_val, compacted_at=$compacted_val"
fi

#===============================================================================
# Test 6: write_boot_timestamp() — handles absent state file gracefully
#===============================================================================
begin_test "write_boot_timestamp: no-op when state file missing"

rm -f "$TEST_STATE_FILE"
run_and_capture_rc write_boot_timestamp
# Should not fail even if state file is absent
if [[ $RC -eq 0 ]]; then
    pass
else
    fail "expected 0, got $RC"
fi

#===============================================================================
# Test 7: write_boot_stamp() from claude-persistent.sh
#===============================================================================
begin_test "write_boot_stamp (claude-persistent.sh): writes booted_at to state file"

# Source write_boot_stamp from claude-persistent.sh
export LOBSTER_MESSAGES="$TEST_MESSAGES"
export LOBSTER_WORKSPACE="$TEST_TMPDIR"
state_file_for_persistent="$TEST_STATE_FILE"

# Extract and eval write_boot_stamp function
eval "$(grep -A 35 '^write_boot_stamp\(\)' "$PERSISTENT_SCRIPT" | head -36)" 2>/dev/null || true

# Fake the STATE_FILE variable that claude-persistent.sh uses
STATE_FILE="$state_file_for_persistent"

# Pre-create a state file with known content
cat > "$STATE_FILE" << EOF
{"mode": "active", "pid": 555}
EOF

# Call the function directly (using the log() function stub)
log() { true; }  # stub the log function from claude-persistent.sh
write_boot_stamp

booted_val=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d.get('booted_at','MISSING'))")
if [[ "$booted_val" != "MISSING" ]]; then
    pass
else
    fail "booted_at not written by write_boot_stamp"
fi

#===============================================================================
# Summary
#===============================================================================
echo ""
echo -e "${BOLD}Results: $PASS/$TOTAL passed${NC}"
if [[ $FAIL -gt 0 ]]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
else
    echo -e "${GREEN}All tests passed${NC}"
    exit 0
fi
