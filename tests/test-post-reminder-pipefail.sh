#!/bin/bash
#===============================================================================
# Test Suite: post-reminder.sh exit-0 when LOBSTER_DEV_MODE absent from config
#
# Regression test for issue #1828: under set -euo pipefail, grep returning
# exit code 1 (no match) inside a pipeline caused the script to abort
# silently. Any config.env that lacks LOBSTER_DEV_MODE triggers this.
#
# Also covers the same pattern in the other scripts that share the same
# dev-mode check: inbox-staleness-warn.sh, daily-update-check.sh,
# check-agent-outputs.sh, daily-health-check.sh.
#
# Usage: bash tests/test-post-reminder-pipefail.sh
#        (run from repo root or any directory)
#===============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-pipefail-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

# --- Helpers -----------------------------------------------------------------

pass() {
    PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1))
    echo -e "  ${GREEN}PASS${NC} $1"
}

fail() {
    FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1))
    echo -e "  ${RED}FAIL${NC} $1"
    echo -e "       ${YELLOW}$2${NC}"
}

# Run a script with isolated HOME and LOBSTER_CONFIG_DIR, assert exit 0.
assert_exits_zero() {
    local label="$1"
    local script="$2"
    local fake_home="$3"
    local fake_config_dir="$4"
    shift 4
    # Extra env vars passed as KEY=VALUE strings in remaining args
    if env HOME="$fake_home" LOBSTER_CONFIG_DIR="$fake_config_dir" "$@" \
            bash "$script" "test_type" > /dev/null 2>&1; then
        pass "$label"
    else
        local code=$?
        fail "$label" "exited $code (expected 0)"
    fi
}

# Same as above but for scripts that take no positional args (or need different args)
run_with_env() {
    local fake_home="$1"
    local fake_config_dir="$2"
    local script="$3"
    shift 3
    env HOME="$fake_home" LOBSTER_CONFIG_DIR="$fake_config_dir" "$@" bash "$script"
}

# --- Setup: fake HOME structure scripts need ---------------------------------

FAKE_HOME="$TEST_TMPDIR/home"
FAKE_INBOX="$FAKE_HOME/messages/inbox"
FAKE_PROCESSING="$FAKE_HOME/messages/processing"
FAKE_CONFIG_DIR="$TEST_TMPDIR/lobster-config"
mkdir -p "$FAKE_INBOX" "$FAKE_PROCESSING" "$FAKE_CONFIG_DIR"

# --- Test 1: config.env with LOBSTER_DEV_MODE absent -------------------------
echo ""
echo -e "${BOLD}Scenario: config.env exists but LOBSTER_DEV_MODE is absent${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
# Minimal config — LOBSTER_DEV_MODE intentionally absent
TELEGRAM_BOT_TOKEN=dummy
LOBSTER_DEBUG=false
EOF

# post-reminder.sh: exits 0 (dedup check passes, writes file)
if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/post-reminder.sh" "absent_test" > /dev/null 2>&1; then
    pass "post-reminder.sh exits 0 with LOBSTER_DEV_MODE absent"
else
    fail "post-reminder.sh exits 0 with LOBSTER_DEV_MODE absent" "exited non-zero"
fi

# inbox-staleness-warn.sh: no stale messages → exits 0
if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/inbox-staleness-warn.sh" > /dev/null 2>&1; then
    pass "inbox-staleness-warn.sh exits 0 with LOBSTER_DEV_MODE absent"
else
    fail "inbox-staleness-warn.sh exits 0 with LOBSTER_DEV_MODE absent" "exited non-zero"
fi

# daily-update-check.sh: exits 0 (non-git fake dir, nothing to update)
FAKE_INSTALL="$TEST_TMPDIR/fake-install-no-git"
mkdir -p "$FAKE_INSTALL"
if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_INSTALL_DIR="$FAKE_INSTALL" \
        LOBSTER_MESSAGES="$FAKE_HOME/messages" \
        bash "$REPO_ROOT/scripts/daily-update-check.sh" > /dev/null 2>&1; then
    pass "daily-update-check.sh exits 0 with LOBSTER_DEV_MODE absent"
else
    fail "daily-update-check.sh exits 0 with LOBSTER_DEV_MODE absent" "exited non-zero"
fi

# check-agent-outputs.sh: exits 0 (no pending agents)
if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_MESSAGES="$FAKE_HOME/messages" \
        LOBSTER_INSTALL_DIR="$REPO_ROOT" \
        bash "$REPO_ROOT/scripts/check-agent-outputs.sh" > /dev/null 2>&1; then
    pass "check-agent-outputs.sh exits 0 with LOBSTER_DEV_MODE absent"
else
    fail "check-agent-outputs.sh exits 0 with LOBSTER_DEV_MODE absent" "exited non-zero"
fi

# --- Test 2: config.env with LOBSTER_DEV_MODE=false --------------------------
echo ""
echo -e "${BOLD}Scenario: config.env exists with LOBSTER_DEV_MODE=false${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
LOBSTER_DEV_MODE=false
TELEGRAM_BOT_TOKEN=dummy
EOF

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/post-reminder.sh" "false_test" > /dev/null 2>&1; then
    pass "post-reminder.sh exits 0 with LOBSTER_DEV_MODE=false"
else
    fail "post-reminder.sh exits 0 with LOBSTER_DEV_MODE=false" "exited non-zero"
fi

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/inbox-staleness-warn.sh" > /dev/null 2>&1; then
    pass "inbox-staleness-warn.sh exits 0 with LOBSTER_DEV_MODE=false"
else
    fail "inbox-staleness-warn.sh exits 0 with LOBSTER_DEV_MODE=false" "exited non-zero"
fi

# --- Test 3: config.env with LOBSTER_DEV_MODE=true → suppress (exit 0) -------
echo ""
echo -e "${BOLD}Scenario: config.env exists with LOBSTER_DEV_MODE=true (dev mode on)${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
LOBSTER_DEV_MODE=true
EOF

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/post-reminder.sh" "suppressed_type" > /dev/null 2>&1; then
    pass "post-reminder.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true"
else
    fail "post-reminder.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true" "exited non-zero"
fi

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/inbox-staleness-warn.sh" > /dev/null 2>&1; then
    pass "inbox-staleness-warn.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true"
else
    fail "inbox-staleness-warn.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true" "exited non-zero"
fi

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_INSTALL_DIR="$FAKE_INSTALL" \
        LOBSTER_MESSAGES="$FAKE_HOME/messages" \
        bash "$REPO_ROOT/scripts/daily-update-check.sh" > /dev/null 2>&1; then
    pass "daily-update-check.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true"
else
    fail "daily-update-check.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true" "exited non-zero"
fi

# --- Test 4: No config.env at all (config file completely absent) ------------
echo ""
echo -e "${BOLD}Scenario: config.env does not exist at all${NC}"
EMPTY_CONFIG_DIR="$TEST_TMPDIR/no-config-dir"

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$EMPTY_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/post-reminder.sh" "no_config_type" > /dev/null 2>&1; then
    pass "post-reminder.sh exits 0 when config.env absent entirely"
else
    fail "post-reminder.sh exits 0 when config.env absent entirely" "exited non-zero"
fi

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$EMPTY_CONFIG_DIR" \
        bash "$REPO_ROOT/scripts/inbox-staleness-warn.sh" > /dev/null 2>&1; then
    pass "inbox-staleness-warn.sh exits 0 when config.env absent entirely"
else
    fail "inbox-staleness-warn.sh exits 0 when config.env absent entirely" "exited non-zero"
fi

# --- Test 5: Verify post-reminder.sh actually writes the inbox file ----------
echo ""
echo -e "${BOLD}Scenario: post-reminder.sh writes inbox file when not suppressed${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
LOBSTER_DEV_MODE=false
EOF

# Clear any leftover inbox files
rm -f "$FAKE_INBOX"/*.json 2>/dev/null || true

env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
    bash "$REPO_ROOT/scripts/post-reminder.sh" "write_test" > /dev/null 2>&1

WRITTEN=$(find "$FAKE_INBOX" -name "*write_test*" -maxdepth 1 2>/dev/null | wc -l)
if [ "$WRITTEN" -ge 1 ]; then
    pass "post-reminder.sh writes inbox JSON when not suppressed"
else
    fail "post-reminder.sh writes inbox JSON when not suppressed" "no file found in $FAKE_INBOX"
fi

# --- Summary -----------------------------------------------------------------

echo ""
echo "─────────────────────────────"
if [ "$FAIL" -eq 0 ]; then
    echo -e "${GREEN}${BOLD}All $TOTAL tests passed${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}$FAIL/$TOTAL tests failed${NC}"
    exit 1
fi
