#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Load-Bearing config.env Keys (issue #2200)
#
# Tests for check_config_keys():
#   1. Returns 0 (GREEN) and logs OK when all REQUIRED_CONFIG_KEYS are present
#   2. Returns 1 and logs an error listing the missing key(s) when one is absent
#   3. Sends a deduped Telegram alert (not the raw one) when key(s) are missing
#   4. Returns 1 and logs an error when config.env itself is missing
#   5. A key present but empty (KEY=) is treated as missing, not present
#
# Usage: bash tests/test-health-check-config-keys.sh
#===============================================================================

set -eE

run_and_capture_rc() {
    "$@" && RC=$? || RC=$?
}

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
HEALTH_SCRIPT="$SCRIPT_DIR/health-check-v3.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-config-keys-test-XXXXXX)
TEST_LOG_DIR="$TEST_TMPDIR/logs"
TEST_LOG="$TEST_LOG_DIR/health-check.log"
TEST_CONFIG_ENV="$TEST_TMPDIR/config.env"

cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

mkdir -p "$TEST_LOG_DIR"

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); echo -e "  ${GREEN}PASS${NC} $1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); echo -e "  ${RED}FAIL${NC} $1"; echo -e "       ${YELLOW}${2:-}${NC}"; }

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then pass "$label"; else fail "$label" "expected: $expected / got: $actual"; fi
}

assert_contains() {
    local label="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then pass "$label"; else fail "$label" "expected to contain '$needle', got:\n$haystack"; fi
}

assert_not_contains() {
    local label="$1" haystack="$2" needle="$3"
    if [[ "$haystack" != *"$needle"* ]]; then pass "$label"; else fail "$label" "expected NOT to contain '$needle', got:\n$haystack"; fi
}

# Extract the specific named function under test, plus its logging deps,
# rather than sourcing the whole script (which has top-level side effects
# like acquire_lock and an early LOBSTER_ENV exit). Mirrors the convention
# in tests/test-health-check-boot-grace.sh.
source_health_check_functions() {
    LOG_FILE="$TEST_LOG"
    CONFIG_ENV="$TEST_CONFIG_ENV"

    eval "$(sed -n '/^log()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
    log_info()  { log "INFO"  "$1"; }
    log_warn()  { log "WARN"  "$1"; }
    log_error() { log "ERROR" "$1"; }

    # Spy instead of the real Telegram sender - records calls without
    # needing ALERT_DEDUP_DIR/cooldown state.
    DEDUPED_ALERTS_SENT=()
    send_telegram_alert_deduped() { DEDUPED_ALERTS_SENT+=("$1"); }

    eval "$(sed -n '/^REQUIRED_CONFIG_KEYS=(/,/^)/p' "$HEALTH_SCRIPT")" 2>/dev/null
    eval "$(sed -n '/^check_config_keys()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null
}

source_health_check_functions

echo ""
echo -e "${BOLD}Health Check: config.env Load-Bearing Keys Tests${NC}"
echo "script: $HEALTH_SCRIPT"
echo "required keys: ${REQUIRED_CONFIG_KEYS[*]}"
echo ""

#===============================================================================
# Test 1: all required keys present -> GREEN
#===============================================================================
echo "-- All required keys present --"
: > "$TEST_LOG"
{
    for k in "${REQUIRED_CONFIG_KEYS[@]}"; do
        echo "${k}=some-value"
    done
} > "$TEST_CONFIG_ENV"

DEDUPED_ALERTS_SENT=()
run_and_capture_rc check_config_keys
assert_eq "returns 0 when all keys present" "0" "$RC"
log_contents=$(cat "$TEST_LOG")
assert_contains "logs CONFIG KEYS OK" "$log_contents" "CONFIG KEYS OK"
assert_eq "no alert sent when all keys present" "0" "${#DEDUPED_ALERTS_SENT[@]}"

#===============================================================================
# Test 2: one required key missing -> RED (advisory) + error log + alert
#===============================================================================
echo ""
echo "-- One required key missing --"
: > "$TEST_LOG"
missing_key="${REQUIRED_CONFIG_KEYS[0]}"
{
    for k in "${REQUIRED_CONFIG_KEYS[@]}"; do
        [[ "$k" == "$missing_key" ]] && continue
        echo "${k}=some-value"
    done
} > "$TEST_CONFIG_ENV"

DEDUPED_ALERTS_SENT=()
run_and_capture_rc check_config_keys
assert_eq "returns 1 when a required key is missing" "1" "$RC"
log_contents=$(cat "$TEST_LOG")
assert_contains "logs CONFIG KEYS MISSING with the missing key name" "$log_contents" "$missing_key"
assert_eq "sends exactly one deduped alert" "1" "${#DEDUPED_ALERTS_SENT[@]}"
assert_contains "deduped alert uses the 'config-keys-missing' dedup key" "${DEDUPED_ALERTS_SENT[0]:-}" "config-keys-missing"

#===============================================================================
# Test 3: config.env itself missing
#===============================================================================
echo ""
echo "-- config.env file missing --"
: > "$TEST_LOG"
rm -f "$TEST_CONFIG_ENV"

run_and_capture_rc check_config_keys
assert_eq "returns 1 when config.env is missing" "1" "$RC"
log_contents=$(cat "$TEST_LOG")
assert_contains "logs that config.env was not found" "$log_contents" "config.env not found"

#===============================================================================
# Test 4: key present but empty is treated as missing
#===============================================================================
echo ""
echo "-- Required key present but empty --"
: > "$TEST_LOG"
empty_key="${REQUIRED_CONFIG_KEYS[0]}"
{
    echo "${empty_key}="
    for k in "${REQUIRED_CONFIG_KEYS[@]}"; do
        [[ "$k" == "$empty_key" ]] && continue
        echo "${k}=some-value"
    done
} > "$TEST_CONFIG_ENV"

run_and_capture_rc check_config_keys
assert_eq "returns 1 when a required key is present but empty" "1" "$RC"
log_contents=$(cat "$TEST_LOG")
assert_contains "logs the empty key as missing" "$log_contents" "$empty_key"

#===============================================================================
# Summary
#===============================================================================
echo ""
echo -e "${BOLD}Results: $PASS/$TOTAL passed${NC}"
if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
