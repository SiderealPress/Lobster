#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - SESSION_AGE_LIMIT_SECONDS default (issue #2196)
#
# check_session_age() (tested separately in test-health-check-session-age.sh)
# is only half the story: this suite tests the *config resolution* that feeds
# SESSION_AGE_LIMIT_SECONDS into it.
#
# Background: PR #2197 fixed a real bug (claude-wrapper.exp never wrote
# dispatcher.pid, so check_session_age() could never find a PID to SIGTERM in
# debug-mode launches). But the restart this fix re-enables is gated by
# SESSION_AGE_LIMIT_SECONDS, which used to default to 7200 (2h) — justified by
# a claimed "CC enforces a hard 7440s session lifetime" that issue #2157 found
# was never confirmed (2 data points, no Anthropic doc citation, a more likely
# alternative explanation already fixed by #2074) and that a 90+ hour live
# dispatcher session directly contradicts. Landing #2197's PID fix alongside
# the old 7200s default would have silently reintroduced forced 2-hour
# restarts for a debunked reason.
#
# Fix (issue #2196): SESSION_AGE_LIMIT_SECONDS now DEFAULTS TO 0 (disabled).
# The mechanism remains available and functional (PID fix included) for an
# operator who deliberately opts in via LOBSTER_SESSION_AGE_LIMIT_SECONDS.
#
# Tests:
#   1. No env var, no config.env entry -> SESSION_AGE_LIMIT_SECONDS=0 (disabled)
#   2. LOBSTER_SESSION_AGE_LIMIT_SECONDS env var set -> that value wins
#   3. config.env sets LOBSTER_SESSION_AGE_LIMIT_SECONDS -> that value is read
#   4. env var takes precedence over a conflicting config.env value
#
# Usage: bash tests/test-health-check-session-age-default.sh
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

TEST_TMPDIR=$(mktemp -d /tmp/lobster-session-age-default-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

#===============================================================================
# Named constant (from the spec — never hardcode magic values in tests)
#===============================================================================
EXPECTED_DEFAULT_SESSION_AGE_LIMIT_SECONDS=0   # issue #2196: disabled by default

# Extract just the two config-resolution blocks for SESSION_AGE_LIMIT_SECONDS
# from the real script (same "extract, don't reimplement" approach used by
# test-health-check-session-age.sh), so this test exercises the actual
# production logic rather than a hand-copied reimplementation of it.
CONFIG_BLOCK=$(
    sed -n '/^# Session age limit: proactive SIGTERM restart mechanism/,/^SESSION_AGE_LIMIT_SECONDS="\${LOBSTER_SESSION_AGE_LIMIT_SECONDS:-0}"$/p' "$HEALTH_SCRIPT"
    sed -n '/^# Read LOBSTER_SESSION_AGE_LIMIT_SECONDS from config.env/,/^SESSION_AGE_LIMIT_SECONDS="\${LOBSTER_SESSION_AGE_LIMIT_SECONDS:-0}"$/p' "$HEALTH_SCRIPT"
)

if [[ -z "$CONFIG_BLOCK" ]]; then
    echo "FATAL: could not extract SESSION_AGE_LIMIT_SECONDS config-resolution block from $HEALTH_SCRIPT"
    echo "(anchors may be out of date — see test file header)"
    exit 1
fi

# Run the extracted config-resolution block in a clean subshell with a
# controlled environment, and print the resulting SESSION_AGE_LIMIT_SECONDS.
resolve_session_age_limit() {
    local config_env_path="$1"
    shift
    env -i \
        HOME="$HOME" \
        CONFIG_ENV="$config_env_path" \
        "$@" \
        bash -c "$CONFIG_BLOCK"$'\necho "$SESSION_AGE_LIMIT_SECONDS"'
}

NO_CONFIG_ENV="$TEST_TMPDIR/no-such-config.env"

echo ""
echo "=== Health Check SESSION_AGE_LIMIT_SECONDS Default Tests ==="
echo ""

# 1. No env var, no config.env -> default is 0 (disabled)
begin_test "no_override_defaults_to_disabled"
result=$(resolve_session_age_limit "$NO_CONFIG_ENV")
if [[ "$result" -eq "$EXPECTED_DEFAULT_SESSION_AGE_LIMIT_SECONDS" ]]; then
    pass
else
    fail "expected $EXPECTED_DEFAULT_SESSION_AGE_LIMIT_SECONDS, got '$result'"
fi

# 2. LOBSTER_SESSION_AGE_LIMIT_SECONDS env var set -> opt-in value wins
begin_test "env_var_opts_in_to_explicit_value"
result=$(resolve_session_age_limit "$NO_CONFIG_ENV" LOBSTER_SESSION_AGE_LIMIT_SECONDS=3600)
if [[ "$result" -eq 3600 ]]; then
    pass
else
    fail "expected 3600, got '$result'"
fi

# 3. config.env sets LOBSTER_SESSION_AGE_LIMIT_SECONDS -> value is read from file
begin_test "config_env_opts_in_to_explicit_value"
CONFIG_ENV_FILE="$TEST_TMPDIR/config.env"
echo 'LOBSTER_SESSION_AGE_LIMIT_SECONDS=1800' > "$CONFIG_ENV_FILE"
result=$(resolve_session_age_limit "$CONFIG_ENV_FILE")
if [[ "$result" -eq 1800 ]]; then
    pass
else
    fail "expected 1800, got '$result'"
fi
rm -f "$CONFIG_ENV_FILE"

# 4. env var takes precedence over a conflicting config.env value
begin_test "env_var_takes_precedence_over_config_env"
CONFIG_ENV_FILE="$TEST_TMPDIR/config.env"
echo 'LOBSTER_SESSION_AGE_LIMIT_SECONDS=1800' > "$CONFIG_ENV_FILE"
result=$(resolve_session_age_limit "$CONFIG_ENV_FILE" LOBSTER_SESSION_AGE_LIMIT_SECONDS=9999)
if [[ "$result" -eq 9999 ]]; then
    pass
else
    fail "expected 9999, got '$result'"
fi
rm -f "$CONFIG_ENV_FILE"

echo ""
echo "=== Results: $PASS/$TOTAL passed ==="
echo ""

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
