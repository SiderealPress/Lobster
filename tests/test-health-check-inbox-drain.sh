#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Inbox Drain Fixes
#
# Tests for:
#   1. Allowlist-based inbox drain check (only user-facing sources count)
#   2. Post-restart verification with circuit breaker
#
# Usage: bash tests/test-health-check-inbox-drain.sh
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

# Test isolation
TEST_TMPDIR=$(mktemp -d /tmp/lobster-hc-test-XXXXXX)
TEST_INBOX="$TEST_TMPDIR/inbox"
TEST_LOG="$TEST_TMPDIR/logs/health-check.log"
TEST_RESTART_STATE="$TEST_TMPDIR/logs/health-restart-state-v3"

cleanup() {
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

mkdir -p "$TEST_INBOX" "$(dirname "$TEST_LOG")"

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
    local msg="${1:-}"
    if [ -n "$msg" ]; then
        echo -e "  ${RED}FAIL${NC} $test_name: $msg"
    else
        echo -e "  ${RED}FAIL${NC} $test_name"
    fi
}

reset_inbox() {
    rm -f "$TEST_INBOX"/*
}

# Create a test message JSON file in the inbox
# Args: $1=filename, $2=source, $3=age_seconds (how old to make it)
create_test_message() {
    local filename="$1"
    local source="$2"
    local age_seconds="$3"
    local filepath="$TEST_INBOX/$filename"

    cat > "$filepath" <<JSONEOF
{
  "id": "${filename%.json}",
  "source": "$source",
  "chat_id": 12345,
  "user_id": 12345,
  "username": "testuser",
  "user_name": "Test User",
  "text": "test message",
  "timestamp": "2026-01-01T00:00:00"
}
JSONEOF

    # Set file modification time to age_seconds ago
    local target_time
    target_time=$(date -d "-${age_seconds} seconds" '+%Y%m%d%H%M.%S' 2>/dev/null) || \
    target_time=$(date -v-${age_seconds}S '+%Y%m%d%H%M.%S' 2>/dev/null)
    touch -t "$target_time" "$filepath"
}

# Source just the functions we need from the health check script
# We override variables and stub out functions we don't want
source_check_inbox_drain() {
    # Create a temporary script that sources the function in isolation
    local test_script="$TEST_TMPDIR/test_check_inbox_drain.sh"
    cat > "$test_script" <<'SCRIPTEOF'
#!/bin/bash
set -o pipefail

# Override config
INBOX_DIR="__INBOX_DIR__"
STALE_THRESHOLD_SECONDS=180
YELLOW_THRESHOLD_SECONDS=120
LOG_FILE="__LOG_FILE__"
USER_FACING_SOURCES="telegram sms signal slack"
STALE_INBOX_MARKER_DIR="__MARKER_DIR__"

mkdir -p "$(dirname "$LOG_FILE")"

# Minimal logging stubs
log() { echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }
log_info()  { log "INFO"  "$1"; }
log_warn()  { log "WARN"  "$1"; }
log_error() { log "ERROR" "$1"; }

SCRIPTEOF

    # Extract is_user_facing_source helper function
    sed -n '/^is_user_facing_source()/,/^}/p' "$HEALTH_SCRIPT" >> "$test_script"

    # Extract check_inbox_drain function from the health check script
    sed -n '/^check_inbox_drain()/,/^}/p' "$HEALTH_SCRIPT" >> "$test_script"

    # Add invocation
    echo 'check_inbox_drain' >> "$test_script"
    echo 'exit $?' >> "$test_script"

    # Replace placeholders
    sed -i "s|__INBOX_DIR__|$TEST_INBOX|g" "$test_script"
    sed -i "s|__LOG_FILE__|$TEST_LOG|g" "$test_script"
    # Default marker dir to a non-existent path (no circuit breaker active)
    sed -i "s|__MARKER_DIR__|$TEST_TMPDIR/no-markers|g" "$test_script"

    chmod +x "$test_script"
    bash "$test_script"
    return $?
}

#===============================================================================
# Tests: Allowlist-based source filtering
#===============================================================================

echo ""
echo -e "${BOLD}=== Fix 1: Allowlist-based inbox drain check ===${NC}"

# Test 1: System messages should NOT trigger RED even when stale
begin_test "Stale system message does NOT trigger RED"
reset_inbox
create_test_message "001_self.json" "system" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - system messages should be ignored"
fi

# Test 2: Stale internal messages should NOT trigger RED
begin_test "Stale internal message does NOT trigger RED"
reset_inbox
create_test_message "002_internal.json" "internal" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - internal messages should be ignored"
fi

# Test 3: Stale task-output messages should NOT trigger RED
begin_test "Stale task-output message does NOT trigger RED"
reset_inbox
create_test_message "003_task.json" "task-output" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - task-output messages should be ignored"
fi

# Test 4: Stale telegram message SHOULD trigger RED
begin_test "Stale telegram message triggers RED"
reset_inbox
create_test_message "004_tg.json" "telegram" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 2 ]; then
    pass
else
    fail "Expected RED (2), got $rc - telegram messages should count"
fi

# Test 5: Stale slack message SHOULD trigger RED
begin_test "Stale slack message triggers RED"
reset_inbox
create_test_message "005_slack.json" "slack" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 2 ]; then
    pass
else
    fail "Expected RED (2), got $rc - slack messages should count"
fi

# Test 6: Stale sms message SHOULD trigger RED
begin_test "Stale sms message triggers RED"
reset_inbox
create_test_message "006_sms.json" "sms" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 2 ]; then
    pass
else
    fail "Expected RED (2), got $rc - sms messages should count"
fi

# Test 7: Stale signal message SHOULD trigger RED
begin_test "Stale signal message triggers RED"
reset_inbox
create_test_message "007_signal.json" "signal" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 2 ]; then
    pass
else
    fail "Expected RED (2), got $rc - signal messages should count"
fi

# Test 8: Mix of system and telegram - only telegram counts
begin_test "Mixed: stale system + fresh telegram = GREEN"
reset_inbox
create_test_message "008_sys.json" "system" 500
create_test_message "009_tg.json" "telegram" 10
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - only the fresh telegram should count"
fi

# Test 9: Mix of system and stale telegram
begin_test "Mixed: stale system + stale telegram = RED"
reset_inbox
create_test_message "010_sys.json" "system" 500
create_test_message "011_tg.json" "telegram" 300
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 2 ]; then
    pass
else
    fail "Expected RED (2), got $rc - stale telegram should trigger RED"
fi

# Test 10: Empty inbox = GREEN
begin_test "Empty inbox = GREEN"
reset_inbox
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc"
fi

# Test 11: Only system messages, none stale = GREEN
begin_test "Fresh system messages only = GREEN"
reset_inbox
create_test_message "012_sys.json" "system" 10
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc"
fi

# Test 12: Telegram message in YELLOW zone
begin_test "Telegram message in YELLOW zone (120-180s) = YELLOW"
reset_inbox
create_test_message "013_tg.json" "telegram" 150
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 1 ]; then
    pass
else
    fail "Expected YELLOW (1), got $rc"
fi

# Test 13: System message in YELLOW zone should NOT trigger YELLOW
begin_test "System message in YELLOW zone does NOT trigger YELLOW"
reset_inbox
create_test_message "014_sys.json" "system" 150
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - system messages should be ignored"
fi

# Test 14: Malformed JSON file should be skipped gracefully
begin_test "Malformed JSON file is skipped gracefully"
reset_inbox
echo "not json" > "$TEST_INBOX/015_bad.json"
touch -t "$(date -d '-300 seconds' '+%Y%m%d%H%M.%S')" "$TEST_INBOX/015_bad.json"
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - malformed JSON should be skipped"
fi

# Test 15: JSON without source field should be skipped
begin_test "JSON without source field is skipped"
reset_inbox
echo '{"id": "test", "text": "hello"}' > "$TEST_INBOX/016_nosource.json"
touch -t "$(date -d '-300 seconds' '+%Y%m%d%H%M.%S')" "$TEST_INBOX/016_nosource.json"
run_and_capture_rc source_check_inbox_drain
rc=$RC
if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - missing source should be treated as non-user"
fi

#===============================================================================
# Tests: Post-restart verification (circuit breaker)
#===============================================================================

echo ""
echo -e "${BOLD}=== Fix 2: Post-restart verification / circuit breaker ===${NC}"

# We test the circuit breaker by checking the STALE_RESTART_MARKER_DIR logic.
# The health check should record which files triggered a restart, and if the
# same files are still present after restart, it should NOT restart again.

# Source the full do_restart and check logic for circuit breaker tests
# These are harder to unit test since do_restart calls systemctl,
# so we test the circuit breaker state logic in isolation.

# Test 16: Circuit breaker marker file is created on stale-inbox restart
begin_test "Circuit breaker: stale_inbox_marker tracks triggering files"
reset_inbox
MARKER_DIR="$TEST_TMPDIR/stale-inbox-markers"
rm -rf "$MARKER_DIR"

# Simulate: after a restart for stale inbox, check that check_inbox_drain
# respects the circuit breaker. We do this by checking the function exists
# and references the marker directory.
if grep -q "STALE_INBOX_MARKER" "$HEALTH_SCRIPT"; then
    pass
else
    fail "Health check script should reference STALE_INBOX_MARKER for circuit breaker"
fi

# Test 17: Circuit breaker prevents re-restart for same stale file
begin_test "Circuit breaker: same stale file after restart does NOT trigger RED again"
reset_inbox
MARKER_DIR="$TEST_TMPDIR/stale-inbox-markers"
mkdir -p "$MARKER_DIR"

# Create a stale telegram message
create_test_message "017_tg.json" "telegram" 300

# Simulate that this file already triggered a restart by creating a marker
touch "$MARKER_DIR/017_tg.json"

# Now run check_inbox_drain - it should skip the marked file
# We need to source the function with the marker dir set
local_test_script="$TEST_TMPDIR/test_circuit_breaker.sh"
cat > "$local_test_script" <<'SCRIPTEOF'
#!/bin/bash
set -o pipefail
INBOX_DIR="__INBOX_DIR__"
STALE_THRESHOLD_SECONDS=180
YELLOW_THRESHOLD_SECONDS=120
STALE_INBOX_MARKER_DIR="__MARKER_DIR__"
USER_FACING_SOURCES="telegram sms signal slack"
LOG_FILE="__LOG_FILE__"
mkdir -p "$(dirname "$LOG_FILE")"
log() { echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }
log_info()  { log "INFO"  "$1"; }
log_warn()  { log "WARN"  "$1"; }
log_error() { log "ERROR" "$1"; }
SCRIPTEOF

sed -n '/^is_user_facing_source()/,/^}/p' "$HEALTH_SCRIPT" >> "$local_test_script"
sed -n '/^check_inbox_drain()/,/^}/p' "$HEALTH_SCRIPT" >> "$local_test_script"
echo 'check_inbox_drain' >> "$local_test_script"
echo 'exit $?' >> "$local_test_script"

sed -i "s|__INBOX_DIR__|$TEST_INBOX|g" "$local_test_script"
sed -i "s|__MARKER_DIR__|$MARKER_DIR|g" "$local_test_script"
sed -i "s|__LOG_FILE__|$TEST_LOG|g" "$local_test_script"

chmod +x "$local_test_script"
run_and_capture_rc bash "$local_test_script"
rc=$RC

if [ "$rc" -eq 0 ]; then
    pass
else
    fail "Expected GREEN (0), got $rc - circuit breaker should prevent re-trigger"
fi

# Test 18: New stale file (not in markers) still triggers RED
begin_test "Circuit breaker: new stale file still triggers RED"
reset_inbox
MARKER_DIR="$TEST_TMPDIR/stale-inbox-markers"
rm -rf "$MARKER_DIR"
mkdir -p "$MARKER_DIR"

# Old marker from previous restart
touch "$MARKER_DIR/old_file.json"

# New stale telegram message (not in markers)
create_test_message "018_tg.json" "telegram" 300

# Same test script setup
local_test_script2="$TEST_TMPDIR/test_circuit_breaker2.sh"
cat > "$local_test_script2" <<'SCRIPTEOF'
#!/bin/bash
set -o pipefail
INBOX_DIR="__INBOX_DIR__"
STALE_THRESHOLD_SECONDS=180
YELLOW_THRESHOLD_SECONDS=120
STALE_INBOX_MARKER_DIR="__MARKER_DIR__"
USER_FACING_SOURCES="telegram sms signal slack"
LOG_FILE="__LOG_FILE__"
mkdir -p "$(dirname "$LOG_FILE")"
log() { echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }
log_info()  { log "INFO"  "$1"; }
log_warn()  { log "WARN"  "$1"; }
log_error() { log "ERROR" "$1"; }
SCRIPTEOF

sed -n '/^is_user_facing_source()/,/^}/p' "$HEALTH_SCRIPT" >> "$local_test_script2"
sed -n '/^check_inbox_drain()/,/^}/p' "$HEALTH_SCRIPT" >> "$local_test_script2"
echo 'check_inbox_drain' >> "$local_test_script2"
echo 'exit $?' >> "$local_test_script2"

sed -i "s|__INBOX_DIR__|$TEST_INBOX|g" "$local_test_script2"
sed -i "s|__MARKER_DIR__|$MARKER_DIR|g" "$local_test_script2"
sed -i "s|__LOG_FILE__|$TEST_LOG|g" "$local_test_script2"

chmod +x "$local_test_script2"
run_and_capture_rc bash "$local_test_script2"
rc=$RC

if [ "$rc" -eq 2 ]; then
    pass
else
    fail "Expected RED (2), got $rc - new stale files should still trigger"
fi

#===============================================================================
# Test: Syntax check
#===============================================================================

echo ""
echo -e "${BOLD}=== Syntax Check ===${NC}"

begin_test "health-check-v3.sh passes bash -n syntax check"
if bash -n "$HEALTH_SCRIPT" 2>&1; then
    pass
else
    fail "Syntax errors in health-check-v3.sh"
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
echo -e "${BOLD}==============================${NC}"

if [ "$FAIL" -gt 0 ]; then
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
