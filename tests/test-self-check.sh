#!/bin/bash
#===============================================================================
# Test Suite: Self-Check Reminder System
#
# Tests the three scripts that make up Lobster's self-check system:
#   1. periodic-self-check.sh  (cron-based, primary)
#   2. self-check-reminder.sh  (at-based, hook-triggered)
#   3. schedule-self-check.sh  (hook glue)
#
# Usage: bash tests/test-self-check.sh
#===============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

# Counters
PASS=0
FAIL=0
SKIP=0
TOTAL=0

# Script locations
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
PERIODIC_SCRIPT="$SCRIPT_DIR/periodic-self-check.sh"
REMINDER_SCRIPT="$SCRIPT_DIR/self-check-reminder.sh"
SCHEDULE_SCRIPT="$SCRIPT_DIR/schedule-self-check.sh"

# Test isolation: use temp directories
TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-XXXXXX)
TEST_INBOX="$TEST_TMPDIR/inbox"
TEST_STATE="$TEST_TMPDIR/state"

cleanup() {
    rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

mkdir -p "$TEST_INBOX" "$TEST_STATE"

#===============================================================================
# Test Helpers
#===============================================================================

test_name=""

begin_test() {
    test_name="$1"
    TOTAL=$((TOTAL + 1))
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

skip() {
    SKIP=$((SKIP + 1))
    local msg="${1:-}"
    if [ -n "$msg" ]; then
        echo -e "  ${YELLOW}SKIP${NC} $test_name: $msg"
    else
        echo -e "  ${YELLOW}SKIP${NC} $test_name"
    fi
}

# Clear the test inbox between tests
reset_inbox() {
    rm -f "$TEST_INBOX"/*
}

# Clear the state directory between tests
reset_state() {
    rm -f "$TEST_STATE"/*
}

# Run periodic-self-check.sh with test directories, optionally faking pgrep
# Args:
#   $1 - "with_claude" or "no_claude" (whether to fake claude process)
run_periodic() {
    local fake_claude="$1"

    # Create a modified copy of the script for testing
    local test_script="$TEST_TMPDIR/periodic-self-check-test.sh"
    cp "$PERIODIC_SCRIPT" "$test_script"

    # Override directory variables
    sed -i "s|INBOX_DIR=.*|INBOX_DIR=\"$TEST_INBOX\"|" "$test_script"
    sed -i "s|STATE_DIR=.*|STATE_DIR=\"$TEST_STATE\"|" "$test_script"

    if [ "$fake_claude" = "no_claude" ]; then
        # Replace pgrep check to always fail (no claude running)
        sed -i 's|pgrep -f "claude"|false|' "$test_script"
    else
        # Replace pgrep check to always succeed (claude is running)
        sed -i 's|pgrep -f "claude"|true|' "$test_script"
    fi

    chmod +x "$test_script"
    bash "$test_script"
    return $?
}

# Count files in test inbox
count_inbox() {
    ls "$TEST_INBOX"/*.json 2>/dev/null | wc -l
}

#===============================================================================
# Tests: periodic-self-check.sh
#===============================================================================

echo ""
echo -e "${BOLD}=== periodic-self-check.sh (cron-based) ===${NC}"

# Test 1: Does NOT fire when no claude process is running
begin_test "Does not fire when no claude process running"
reset_inbox
reset_state
run_periodic "no_claude" || true
if [ "$(count_inbox)" -eq 0 ]; then
    pass
else
    fail "Expected 0 messages, got $(count_inbox)"
fi

# Test 2: DOES fire when claude is running and all guards pass
begin_test "Fires when all guards pass (claude running, no dupe, no rate limit)"
reset_inbox
reset_state
run_periodic "with_claude" || true
if [ "$(count_inbox)" -eq 1 ]; then
    pass
else
    fail "Expected 1 message, got $(count_inbox)"
fi

# Test 3: Does NOT fire when self-check already in inbox
begin_test "Does not fire when self-check already in inbox"
reset_inbox
reset_state
# Pre-seed a self-check message
echo '{}' > "$TEST_INBOX/12345_self.json"
run_periodic "with_claude" || true
# Should still be only 1 file (the pre-seeded one)
if [ "$(count_inbox)" -eq 1 ]; then
    pass
else
    fail "Expected 1 message (pre-seeded only), got $(count_inbox)"
fi

# Test 4: Does NOT fire within 2-minute rate limit window
begin_test "Does not fire within 2-minute rate limit window"
reset_inbox
reset_state
# Set last check to 30 seconds ago (within 120-second window)
echo $(($(date +%s) - 30)) > "$TEST_STATE/last-self-check"
run_periodic "with_claude" || true
if [ "$(count_inbox)" -eq 0 ]; then
    pass
else
    fail "Expected 0 messages (rate limited), got $(count_inbox)"
fi

# Test 5: DOES fire when rate limit has expired
begin_test "Fires when rate limit has expired (>120 seconds ago)"
reset_inbox
reset_state
# Set last check to 200 seconds ago (beyond 120-second window)
echo $(($(date +%s) - 200)) > "$TEST_STATE/last-self-check"
run_periodic "with_claude" || true
if [ "$(count_inbox)" -eq 1 ]; then
    pass
else
    fail "Expected 1 message, got $(count_inbox)"
fi

# Test 6: Created message is valid JSON
begin_test "Created message is valid JSON"
reset_inbox
reset_state
run_periodic "with_claude" || true
MSG_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
if [ -n "$MSG_FILE" ] && jq . "$MSG_FILE" > /dev/null 2>&1; then
    pass
else
    fail "Message file is not valid JSON"
fi

# Test 7: Message has all required fields
begin_test "Message has correct fields (id, source, chat_id, user_id, username, user_name, text, timestamp)"
reset_inbox
reset_state
run_periodic "with_claude" || true
MSG_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
if [ -z "$MSG_FILE" ]; then
    fail "No message file created"
else
    MISSING=""
    for field in id source chat_id user_id username user_name text timestamp; do
        if ! jq -e ".$field" "$MSG_FILE" > /dev/null 2>&1; then
            MISSING="$MISSING $field"
        fi
    done
    if [ -z "$MISSING" ]; then
        pass
    else
        fail "Missing fields:$MISSING"
    fi
fi

# Test 8: Message has correct field values
begin_test "Message field values are correct (source=system, text contains self-check)"
reset_inbox
reset_state
run_periodic "with_claude" || true
MSG_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
if [ -z "$MSG_FILE" ]; then
    fail "No message file created"
else
    SOURCE=$(jq -r '.source' "$MSG_FILE")
    TEXT=$(jq -r '.text' "$MSG_FILE")
    USERNAME=$(jq -r '.username' "$MSG_FILE")
    CHAT_ID=$(jq -r '.chat_id' "$MSG_FILE")
    ALL_OK=true
    ERRORS=""
    if [ "$SOURCE" != "system" ]; then
        ALL_OK=false
        ERRORS="source=$SOURCE (expected system)"
    fi
    if [[ "$TEXT" != *"Self-check"* ]]; then
        ALL_OK=false
        ERRORS="$ERRORS text='$TEXT' (expected to contain Self-check)"
    fi
    if [ "$USERNAME" != "lobster-system" ]; then
        ALL_OK=false
        ERRORS="$ERRORS username=$USERNAME (expected lobster-system)"
    fi
    if [ "$CHAT_ID" != "0" ]; then
        ALL_OK=false
        ERRORS="$ERRORS chat_id=$CHAT_ID (expected 0)"
    fi
    if [ "$ALL_OK" = true ]; then
        pass
    else
        fail "$ERRORS"
    fi
fi

# Test 9: Filename matches pattern {epoch_ms}_self.json
begin_test "Filename matches *_self.json pattern"
reset_inbox
reset_state
run_periodic "with_claude" || true
MSG_FILE=$(ls "$TEST_INBOX"/*_self.json 2>/dev/null | head -1)
if [ -n "$MSG_FILE" ]; then
    pass
else
    fail "No file matching *_self.json found"
fi

# Test 10: Rate limit state file gets updated after injection
begin_test "Rate limit state file is updated after injection"
reset_inbox
reset_state
run_periodic "with_claude" || true
if [ -f "$TEST_STATE/last-self-check" ]; then
    STORED=$(cat "$TEST_STATE/last-self-check")
    NOW=$(date +%s)
    DIFF=$((NOW - STORED))
    if [ "$DIFF" -ge 0 ] && [ "$DIFF" -lt 5 ]; then
        pass
    else
        fail "State timestamp is off by ${DIFF}s (expected <5s)"
    fi
else
    fail "State file not created"
fi

# Test 11: Message ID matches filename (consistency)
begin_test "Message ID matches filename"
reset_inbox
reset_state
run_periodic "with_claude" || true
MSG_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
if [ -z "$MSG_FILE" ]; then
    fail "No message file"
else
    FILENAME=$(basename "$MSG_FILE" .json)
    MSG_ID=$(jq -r '.id' "$MSG_FILE")
    if [ "$FILENAME" = "$MSG_ID" ]; then
        pass
    else
        fail "Filename=$FILENAME but id=$MSG_ID"
    fi
fi

#===============================================================================
# Tests: self-check-reminder.sh
#===============================================================================

echo ""
echo -e "${BOLD}=== self-check-reminder.sh (at-based) ===${NC}"

# Test 12: Creates message in correct directory
begin_test "Creates message in inbox directory"
reset_inbox
LOBSTER_MESSAGES="$TEST_TMPDIR" bash "$REMINDER_SCRIPT" > /dev/null 2>&1
if [ "$(count_inbox)" -eq 1 ]; then
    pass
else
    fail "Expected 1 message, got $(count_inbox)"
fi

# Test 13: Created message is valid JSON
begin_test "Created message is valid JSON"
reset_inbox
LOBSTER_MESSAGES="$TEST_TMPDIR" bash "$REMINDER_SCRIPT" > /dev/null 2>&1
MSG_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
if [ -n "$MSG_FILE" ] && jq . "$MSG_FILE" > /dev/null 2>&1; then
    pass
else
    fail "Not valid JSON"
fi

# Test 14: Message fields are correct
begin_test "Message fields are correct"
reset_inbox
LOBSTER_MESSAGES="$TEST_TMPDIR" bash "$REMINDER_SCRIPT" > /dev/null 2>&1
MSG_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
if [ -z "$MSG_FILE" ]; then
    fail "No message file"
else
    SOURCE=$(jq -r '.source' "$MSG_FILE")
    TEXT=$(jq -r '.text' "$MSG_FILE")
    USERNAME=$(jq -r '.username' "$MSG_FILE")
    USER_NAME=$(jq -r '.user_name' "$MSG_FILE")
    ALL_OK=true
    ERRORS=""
    if [ "$SOURCE" != "system" ]; then
        ALL_OK=false
        ERRORS="source=$SOURCE"
    fi
    if [[ "$TEXT" != *"Self-check"* ]]; then
        ALL_OK=false
        ERRORS="$ERRORS text='$TEXT'"
    fi
    if [ "$USERNAME" != "lobster-system" ]; then
        ALL_OK=false
        ERRORS="$ERRORS username=$USERNAME"
    fi
    if [ "$USER_NAME" != "Self-Check" ]; then
        ALL_OK=false
        ERRORS="$ERRORS user_name=$USER_NAME"
    fi
    if [ "$ALL_OK" = true ]; then
        pass
    else
        fail "$ERRORS"
    fi
fi

# Test 15: Filename matches *_self.json pattern
begin_test "Filename matches *_self.json pattern"
reset_inbox
LOBSTER_MESSAGES="$TEST_TMPDIR" bash "$REMINDER_SCRIPT" > /dev/null 2>&1
MSG_FILE=$(ls "$TEST_INBOX"/*_self.json 2>/dev/null | head -1)
if [ -n "$MSG_FILE" ]; then
    pass
else
    fail "No file matching *_self.json"
fi

# Test 16: Prints confirmation message
begin_test "Prints confirmation message to stdout"
reset_inbox
OUTPUT=$(LOBSTER_MESSAGES="$TEST_TMPDIR" bash "$REMINDER_SCRIPT" 2>&1)
if [[ "$OUTPUT" == *"Self-check reminder injected"* ]]; then
    pass
else
    fail "Expected 'Self-check reminder injected' in output, got: $OUTPUT"
fi

#===============================================================================
# Tests: schedule-self-check.sh
#===============================================================================

echo ""
echo -e "${BOLD}=== schedule-self-check.sh (hook glue) ===${NC}"

# Test 17: Schedules an at job
begin_test "Schedules an at job"
# Count existing at jobs
BEFORE=$(atq 2>/dev/null | wc -l)
bash "$SCHEDULE_SCRIPT" 2>/dev/null
sleep 1
AFTER=$(atq 2>/dev/null | wc -l)
if [ "$AFTER" -gt "$BEFORE" ]; then
    pass
    # Clean up: remove the job we just added
    LATEST_JOB=$(atq 2>/dev/null | sort -n | tail -1 | awk '{print $1}')
    if [ -n "$LATEST_JOB" ]; then
        atrm "$LATEST_JOB" 2>/dev/null || true
    fi
else
    fail "at job count did not increase (before=$BEFORE, after=$AFTER)"
fi

#===============================================================================
# Tests: Integration / System
#===============================================================================

echo ""
echo -e "${BOLD}=== Integration Tests ===${NC}"

# Test 18: Cron entry exists for periodic-self-check.sh
begin_test "Cron entry exists for periodic-self-check.sh"
if crontab -l 2>/dev/null | grep -q "periodic-self-check.sh"; then
    pass
else
    fail "No cron entry found for periodic-self-check.sh"
fi

# Test 19: All three scripts are executable
begin_test "All self-check scripts are executable"
ALL_EXEC=true
ERRORS=""
for script in "$PERIODIC_SCRIPT" "$REMINDER_SCRIPT" "$SCHEDULE_SCRIPT"; do
    if [ ! -x "$script" ]; then
        ALL_EXEC=false
        ERRORS="$ERRORS $(basename "$script")"
    fi
done
if [ "$ALL_EXEC" = true ]; then
    pass
else
    fail "Not executable:$ERRORS"
fi

# Test 20: State directory exists or can be created
begin_test "State directory exists at ~/lobster/.state"
if [ -d "$HOME/lobster/.state" ]; then
    pass
else
    fail "State directory not found at $HOME/lobster/.state"
fi

# Test 21: Hook config exists in settings.json
begin_test "Claude Code hook is configured in settings.json"
SETTINGS_FILE="$HOME/.claude/settings.json"
if [ -f "$SETTINGS_FILE" ]; then
    if jq -e '.hooks.PostToolUse[] | select(.matcher == "mcp__lobster-inbox__send_reply")' "$SETTINGS_FILE" > /dev/null 2>&1; then
        pass
    else
        fail "PostToolUse hook for send_reply not found in settings.json"
    fi
else
    fail "settings.json not found at $SETTINGS_FILE"
fi

# Test 22: Hook references the correct script
begin_test "Hook command references schedule-self-check.sh"
SETTINGS_FILE="$HOME/.claude/settings.json"
if [ -f "$SETTINGS_FILE" ]; then
    HOOK_CMD=$(jq -r '.hooks.PostToolUse[] | select(.matcher == "mcp__lobster-inbox__send_reply") | .hooks[0].command' "$SETTINGS_FILE" 2>/dev/null)
    if [[ "$HOOK_CMD" == *"schedule-self-check.sh"* ]]; then
        pass
    else
        fail "Hook command is '$HOOK_CMD', expected path to schedule-self-check.sh"
    fi
else
    fail "settings.json not found"
fi

# Test 23: Messages format matches inbox server expectations (same fields as telegram messages)
begin_test "Self-check message format matches inbox server expectations"
reset_inbox
LOBSTER_MESSAGES="$TEST_TMPDIR" bash "$REMINDER_SCRIPT" > /dev/null 2>&1
MSG_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
if [ -z "$MSG_FILE" ]; then
    fail "No message file"
else
    # Check all fields match the format used by Telegram messages:
    # id, source, chat_id, user_id, username, user_name, text, timestamp
    FIELD_COUNT=$(jq 'keys | length' "$MSG_FILE")
    HAS_ALL=$(jq 'has("id") and has("source") and has("chat_id") and has("user_id") and has("username") and has("user_name") and has("text") and has("timestamp")' "$MSG_FILE")
    if [ "$HAS_ALL" = "true" ] && [ "$FIELD_COUNT" -eq 8 ]; then
        pass
    else
        fail "Field count=$FIELD_COUNT, has_all=$HAS_ALL"
    fi
fi

# Test 24: Both scripts produce identical message format
begin_test "periodic and reminder scripts produce identical message schema"
reset_inbox
reset_state
# Run periodic
run_periodic "with_claude" || true
PERIODIC_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
PERIODIC_KEYS=""
if [ -n "$PERIODIC_FILE" ]; then
    PERIODIC_KEYS=$(jq -r 'keys | sort | join(",")' "$PERIODIC_FILE")
fi

reset_inbox
# Run reminder
LOBSTER_MESSAGES="$TEST_TMPDIR" bash "$REMINDER_SCRIPT" > /dev/null 2>&1
REMINDER_FILE=$(ls "$TEST_INBOX"/*.json 2>/dev/null | head -1)
REMINDER_KEYS=""
if [ -n "$REMINDER_FILE" ]; then
    REMINDER_KEYS=$(jq -r 'keys | sort | join(",")' "$REMINDER_FILE")
fi

if [ -n "$PERIODIC_KEYS" ] && [ "$PERIODIC_KEYS" = "$REMINDER_KEYS" ]; then
    pass
else
    fail "periodic keys=$PERIODIC_KEYS, reminder keys=$REMINDER_KEYS"
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
if [ "$SKIP" -gt 0 ]; then
    echo -e "  ${YELLOW}SKIP: $SKIP${NC}"
fi
echo -e "${BOLD}==============================${NC}"

if [ "$FAIL" -gt 0 ]; then
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
