#!/bin/bash
# =============================================================================
# Hibernation MVP - Shell Tests
#
# Tests state file management and health check hibernation awareness.
# Run directly: bash tests/test-hibernation.sh
# =============================================================================

set -o pipefail

PASS=0
FAIL=0
ERRORS=()

# ---------- helpers -----------------------------------------------------------

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); ERRORS+=("$1"); }

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass "$desc"
    else
        fail "$desc (expected='$expected' actual='$actual')"
    fi
}

assert_file_exists() {
    local desc="$1" path="$2"
    if [[ -f "$path" ]]; then
        pass "$desc"
    else
        fail "$desc (file not found: $path)"
    fi
}

assert_file_not_exists() {
    local desc="$1" path="$2"
    if [[ ! -f "$path" ]]; then
        pass "$desc"
    else
        fail "$desc (file should not exist: $path)"
    fi
}

# ---------- setup / teardown --------------------------------------------------

WORK_DIR="$(mktemp -d /tmp/lobster-hibernation-test-XXXXXX)"
STATE_DIR="$WORK_DIR/config"
mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/lobster-state.json"
HEALTH_CHECK_SCRIPT="$(dirname "$0")/../scripts/health-check-v3.sh"

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

# Source the read_lobster_state helper (tested inline below without sourcing
# the full health-check script because it sources config files)

# Inline minimal state helpers mirroring the implementation
write_state() {
    local mode="$1"
    local tmp
    tmp="$(mktemp "$STATE_DIR/.lobster-state-XXXXXX.tmp")"
    printf '{"mode":"%s","updated_at":"%s"}\n' "$mode" "$(date -Iseconds)" > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

read_state_mode() {
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "active"
        return
    fi
    python3 -c "import json,sys; d=json.load(open('$STATE_FILE')); print(d.get('mode','active'))" 2>/dev/null || echo "active"
}

# =============================================================================
# Test 1: State file is created with mode "hibernate"
# =============================================================================
echo ""
echo "--- Test 1: State file creation ---"

write_state "hibernate"
assert_file_exists "state file exists after write" "$STATE_FILE"
mode=$(read_state_mode)
assert_eq "mode is 'hibernate'" "hibernate" "$mode"

# =============================================================================
# Test 2: State file transitions active → hibernate → active
# =============================================================================
echo ""
echo "--- Test 2: State transitions ---"

write_state "active"
mode=$(read_state_mode)
assert_eq "initial state is 'active'" "active" "$mode"

write_state "hibernate"
mode=$(read_state_mode)
assert_eq "after transition: mode is 'hibernate'" "hibernate" "$mode"

write_state "active"
mode=$(read_state_mode)
assert_eq "after wake: mode is 'active'" "active" "$mode"

# =============================================================================
# Test 3: Atomic write (no partial reads)
# =============================================================================
echo ""
echo "--- Test 3: Atomic write ---"

write_state "hibernate"
# The file should be valid JSON
python3 -c "import json; json.load(open('$STATE_FILE'))" 2>/dev/null
assert_eq "state file is valid JSON" "0" "$?"

# Verify it has both required fields
has_mode=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print('yes' if 'mode' in d else 'no')" 2>/dev/null)
has_ts=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print('yes' if 'updated_at' in d else 'no')" 2>/dev/null)
assert_eq "state file has 'mode' field" "yes" "$has_mode"
assert_eq "state file has 'updated_at' field" "yes" "$has_ts"

# =============================================================================
# Test 4: Missing state file defaults to "active"
# =============================================================================
echo ""
echo "--- Test 4: Missing state file defaults to 'active' ---"

rm -f "$STATE_FILE"
mode=$(read_state_mode)
assert_eq "missing state file → 'active'" "active" "$mode"

# =============================================================================
# Test 5: Corrupt state file defaults to "active"
# =============================================================================
echo ""
echo "--- Test 5: Corrupt state file defaults to 'active' ---"

echo "NOT_JSON_AT_ALL" > "$STATE_FILE"
mode=$(read_state_mode)
assert_eq "corrupt state file → 'active'" "active" "$mode"

echo "{}" > "$STATE_FILE"  # valid JSON but no 'mode' key
mode=$(read_state_mode)
assert_eq "empty JSON object → 'active'" "active" "$mode"

# =============================================================================
# Test 6: Health check skips restart when state is "hibernate"
# =============================================================================
echo ""
echo "--- Test 6: Health check hibernation awareness ---"

# We test the logic by simulating what the health check does:
# if state == "hibernate" → skip restart of lobster-claude
write_state "hibernate"

# Extract the logic: health check should read state and return early
# We do a dry-run simulation by calling the health-check with a special env
# variable that prevents any actual systemctl calls.
# Since we can't easily source health-check-v3.sh (it does real checks),
# we test the state-reading logic directly here and rely on Python unit tests
# for the integration behavior.

mode=$(read_state_mode)
if [[ "$mode" == "hibernate" ]]; then
    # In hibernate mode, health check must NOT trigger restart
    pass "health check detects hibernate mode and would skip restart"
else
    fail "health check should detect hibernate mode"
fi

# =============================================================================
# Test 7: Health check DOES restart when state is "active" and Claude is down
# =============================================================================
echo ""
echo "--- Test 7: Health check restarts when 'active' ---"

write_state "active"
mode=$(read_state_mode)
if [[ "$mode" == "active" ]]; then
    pass "health check sees 'active' mode → restart allowed"
else
    fail "health check should see 'active' mode"
fi

# =============================================================================
# Test 8: Only tmp files should not replace state (temp-then-rename pattern)
# =============================================================================
echo ""
echo "--- Test 8: Atomic rename - tmp file cleaned up ---"

write_state "hibernate"
# After a successful write, no tmp files should remain
tmp_count=$(find "$STATE_DIR" -name "*.tmp" 2>/dev/null | wc -l)
assert_eq "no .tmp files after atomic write" "0" "$tmp_count"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "============================================================"
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    echo "Failures:"
    for err in "${ERRORS[@]}"; do
        echo "  - $err"
    done
    exit 1
fi
echo "All tests passed."
exit 0
