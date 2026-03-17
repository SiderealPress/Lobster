#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - PID Timing / Restart Verification
#
# Tests the retry loop introduced in issue #522:
#   - pgrep -x "claude" is retried up to 3 times with 3-second gaps
#   - Success is declared as soon as the returned PID differs from the
#     pre-restart PID (i.e. a new process appeared)
#   - Failure is declared only after all 3 attempts still return the same PID
#
# The retry loop is extracted and exercised with a mock pgrep that returns
# configurable values per call, so no real systemctl or sleep is needed.
#
# Usage: bash tests/test-health-check-pid-timing.sh
#===============================================================================

set -eE

# Helper: capture exit code without triggering set -e
run_and_capture_rc() {
    "$@" && RC=$? || RC=$?
}

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
HEALTH_SCRIPT="$SCRIPT_DIR/health-check-v3.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-pid-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

#===============================================================================
# Helpers
#===============================================================================

begin_test() {
    TOTAL=$(( TOTAL + 1 ))
    test_name="$1"
}

pass() {
    PASS=$(( PASS + 1 ))
    echo -e "  ${GREEN}PASS${NC} $test_name"
}

fail() {
    FAIL=$(( FAIL + 1 ))
    local msg="${1:-}"
    if [ -n "$msg" ]; then
        echo -e "  ${RED}FAIL${NC} $test_name: $msg"
    else
        echo -e "  ${RED}FAIL${NC} $test_name"
    fi
}

# Build an isolated test script that:
#   1. Stubs pgrep with a sequence of return values (one per call)
#   2. Stubs sleep so the test runs instantly
#   3. Stubs log_info / log_error / log_warn to a file we can inspect
#   4. Sources the PID retry loop verbatim from health-check-v3.sh
#   5. Exits with 0 if pid_changed==true, 1 if pid_changed==false
#
# Args:
#   $1 - pre_restart_pid value (the PID captured before restart)
#   $2 - space-separated list of pgrep return values per call
#        e.g. "1574 1574 9999"  means: call 1→1574, call 2→1574, call 3→9999
build_pid_test_script() {
    local pre_restart_pid="$1"
    local pgrep_sequence="$2"   # space-separated list

    local script="$TEST_TMPDIR/test_pid_retry_$$.sh"
    local state_file="$TEST_TMPDIR/pgrep_call_count_$$.txt"
    echo "0" > "$state_file"

    # Convert sequence to a bash array literal for embedding
    local array_literal="( $pgrep_sequence )"

    cat > "$script" <<SCRIPTEOF
#!/bin/bash
# Stub: pgrep returns a pre-configured sequence of values, one per call
PGREP_SEQUENCE=$array_literal
PGREP_CALL_FILE="$state_file"

pgrep() {
    local idx
    idx=\$(cat "\$PGREP_CALL_FILE")
    local val="\${PGREP_SEQUENCE[\$idx]:-}"
    echo \$(( idx + 1 )) > "\$PGREP_CALL_FILE"
    if [[ -n "\$val" ]]; then
        echo "\$val"
        return 0
    else
        return 1
    fi
}

# Stub: sleep is a no-op so tests run instantly
sleep() { :; }

# Stub: logging to a temp file
LOG_FILE="$TEST_TMPDIR/test.log"
log()       { echo "[\$1] \$2" >> "\$LOG_FILE"; }
log_info()  { log "INFO"  "\$1"; }
log_warn()  { log "WARN"  "\$1"; }
log_error() { log "ERROR" "\$1"; }

# ---- Begin verbatim PID retry block from health-check-v3.sh ----
pre_restart_pid="$pre_restart_pid"
local_post_restart_pid=""
pid_changed=true
pid_check_attempts=0
while [[ \$pid_check_attempts -lt 3 ]]; do
    local_post_restart_pid=\$(pgrep -x "claude" 2>/dev/null | head -1)
    if [[ -z "\$pre_restart_pid" || "\$local_post_restart_pid" != "\$pre_restart_pid" ]]; then
        break
    fi
    pid_check_attempts=\$(( pid_check_attempts + 1 ))
    if [[ \$pid_check_attempts -lt 3 ]]; then
        log_info "PID unchanged after restart (attempt \$pid_check_attempts/3), waiting 3s..."
        sleep 3
    fi
done
if [[ -n "\$pre_restart_pid" && "\$local_post_restart_pid" == "\$pre_restart_pid" ]]; then
    pid_changed=false
    log_error "Restart verification failed: Claude PID \$pre_restart_pid unchanged after 3 attempts — old session may have survived"
fi
# ---- End verbatim PID retry block ----

# Emit result as exit code: 0=pid_changed(success), 1=not changed(failure)
if [[ "\$pid_changed" == true ]]; then
    exit 0
else
    exit 1
fi
SCRIPTEOF

    chmod +x "$script"
    echo "$script"
}

#===============================================================================
# Tests
#===============================================================================

echo ""
echo -e "${BOLD}=== PID Timing: Restart Verification Retry Loop ===${NC}"

# Test 1: New PID on first attempt → success immediately, no retries needed
begin_test "PID changes on first attempt → detected as success"
script=$(build_pid_test_script "1574" "9999")
run_and_capture_rc bash "$script"
if [ "$RC" -eq 0 ]; then
    pass
else
    fail "Expected pid_changed=true (exit 0), got $RC — should succeed on first try"
fi

# Test 2: Old PID twice, then new PID on 3rd attempt → success on 3rd try
begin_test "PID stale ×2 then changes on 3rd attempt → detected as success"
script=$(build_pid_test_script "1574" "1574 1574 9999")
run_and_capture_rc bash "$script"
if [ "$RC" -eq 0 ]; then
    pass
else
    fail "Expected pid_changed=true (exit 0), got $RC — should succeed on 3rd attempt"
fi

# Test 3: PID unchanged across all 3 attempts → failure
begin_test "PID unchanged across all 3 attempts → detected as failure"
script=$(build_pid_test_script "1574" "1574 1574 1574")
run_and_capture_rc bash "$script"
if [ "$RC" -eq 1 ]; then
    pass
else
    fail "Expected pid_changed=false (exit 1), got $RC — all-same PID should be failure"
fi

# Test 4: Old PID on first, new PID on second attempt → success, 3rd call never made
begin_test "PID changes on 2nd attempt → success (no 3rd retry needed)"
# Provide only two values — if a 3rd pgrep call happens, it returns empty
# (which would also be treated as success since empty != pre_restart_pid).
# We verify success is reported, not that exactly N calls were made.
script=$(build_pid_test_script "1574" "1574 9999")
run_and_capture_rc bash "$script"
if [ "$RC" -eq 0 ]; then
    pass
else
    fail "Expected pid_changed=true (exit 0), got $RC — should succeed on 2nd attempt"
fi

# Test 5: Empty pre_restart_pid (no claude running before restart) → always succeeds
begin_test "Empty pre_restart_pid → pid_changed=true regardless of post value"
script=$(build_pid_test_script "" "1574")
run_and_capture_rc bash "$script"
if [ "$RC" -eq 0 ]; then
    pass
else
    fail "Expected pid_changed=true (exit 0), got $RC — empty pre-PID means no comparison"
fi

# Test 6: Empty pre_restart_pid + empty post_restart_pid → still succeeds
# (no pre-restart PID means we can't prove a ghost session, so we don't block)
begin_test "Both pre and post PIDs empty → pid_changed=true (no comparison possible)"
script=$(build_pid_test_script "" "")
run_and_capture_rc bash "$script"
if [ "$RC" -eq 0 ]; then
    pass
else
    fail "Expected pid_changed=true (exit 0), got $RC — no PID to compare, should pass"
fi

# Test 7: Verify that the retry loop makes at most 3 pgrep calls when PID never changes
begin_test "PID never changes → exactly 3 pgrep calls made"
pre_pid="1574"
pgrep_seq="1574 1574 1574"
state_file="$TEST_TMPDIR/call_count_t7.txt"
echo "0" > "$state_file"

script="$TEST_TMPDIR/test_pid_call_count.sh"
cat > "$script" <<SCRIPTEOF
#!/bin/bash
PGREP_SEQUENCE=( $pgrep_seq )
PGREP_CALL_FILE="$state_file"

pgrep() {
    local idx
    idx=\$(cat "\$PGREP_CALL_FILE")
    local val="\${PGREP_SEQUENCE[\$idx]:-}"
    echo \$(( idx + 1 )) > "\$PGREP_CALL_FILE"
    [[ -n "\$val" ]] && echo "\$val" && return 0
    return 1
}
sleep() { :; }
LOG_FILE="$TEST_TMPDIR/t7.log"
log()       { echo "[\$1] \$2" >> "\$LOG_FILE"; }
log_info()  { log "INFO"  "\$1"; }
log_warn()  { log "WARN"  "\$1"; }
log_error() { log "ERROR" "\$1"; }

pre_restart_pid="$pre_pid"
local_post_restart_pid=""
pid_changed=true
pid_check_attempts=0
while [[ \$pid_check_attempts -lt 3 ]]; do
    local_post_restart_pid=\$(pgrep -x "claude" 2>/dev/null | head -1)
    if [[ -z "\$pre_restart_pid" || "\$local_post_restart_pid" != "\$pre_restart_pid" ]]; then
        break
    fi
    pid_check_attempts=\$(( pid_check_attempts + 1 ))
    if [[ \$pid_check_attempts -lt 3 ]]; then
        log_info "PID unchanged after restart (attempt \$pid_check_attempts/3), waiting 3s..."
        sleep 3
    fi
done
if [[ -n "\$pre_restart_pid" && "\$local_post_restart_pid" == "\$pre_restart_pid" ]]; then
    pid_changed=false
fi
exit 0
SCRIPTEOF
chmod +x "$script"
bash "$script"

calls=$(cat "$state_file")
if [ "$calls" -eq 3 ]; then
    pass
else
    fail "Expected 3 pgrep calls, got $calls"
fi

# Test 8: Retry loop exits early when PID changes — does NOT make extra calls
begin_test "PID changes on 1st attempt → only 1 pgrep call made"
state_file2="$TEST_TMPDIR/call_count_t8.txt"
echo "0" > "$state_file2"

script2="$TEST_TMPDIR/test_pid_call_count2.sh"
cat > "$script2" <<SCRIPTEOF
#!/bin/bash
PGREP_SEQUENCE=( 9999 )
PGREP_CALL_FILE="$state_file2"

pgrep() {
    local idx
    idx=\$(cat "\$PGREP_CALL_FILE")
    local val="\${PGREP_SEQUENCE[\$idx]:-}"
    echo \$(( idx + 1 )) > "\$PGREP_CALL_FILE"
    [[ -n "\$val" ]] && echo "\$val" && return 0
    return 1
}
sleep() { :; }
LOG_FILE="$TEST_TMPDIR/t8.log"
log()       { echo "[\$1] \$2" >> "\$LOG_FILE"; }
log_info()  { log "INFO"  "\$1"; }
log_warn()  { log "WARN"  "\$1"; }
log_error() { log "ERROR" "\$1"; }

pre_restart_pid="1574"
local_post_restart_pid=""
pid_changed=true
pid_check_attempts=0
while [[ \$pid_check_attempts -lt 3 ]]; do
    local_post_restart_pid=\$(pgrep -x "claude" 2>/dev/null | head -1)
    if [[ -z "\$pre_restart_pid" || "\$local_post_restart_pid" != "\$pre_restart_pid" ]]; then
        break
    fi
    pid_check_attempts=\$(( pid_check_attempts + 1 ))
    if [[ \$pid_check_attempts -lt 3 ]]; then
        log_info "PID unchanged after restart (attempt \$pid_check_attempts/3), waiting 3s..."
        sleep 3
    fi
done
exit 0
SCRIPTEOF
chmod +x "$script2"
bash "$script2"

calls2=$(cat "$state_file2")
if [ "$calls2" -eq 1 ]; then
    pass
else
    fail "Expected 1 pgrep call (early exit), got $calls2"
fi

#===============================================================================
# Syntax check
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
