#!/bin/bash
#===============================================================================
# Test Suite: post-reminder.sh exit-0 when LOBSTER_DEV_MODE absent from config
#
# Regression test for issue #1828: under set -euo pipefail, grep returning
# exit code 1 (no match) inside a pipeline caused the script to abort
# silently. Any config.env that lacks LOBSTER_DEV_MODE triggers this.
#
# Also covers the same pattern in all other scripts that share the same
# dev-mode check: inbox-staleness-warn.sh, daily-update-check.sh,
# check-agent-outputs.sh, daily-health-check.sh, periodic-self-check.sh,
# and scheduled-tasks/dispatch-job.sh.
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

# Allow callers to override REPO_ROOT via env var so the test can be pointed
# at an alternative checkout (e.g. main branch) without modifying this file.
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

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

# --- Test 5: daily-health-check.sh -------------------------------------------
#
# daily-health-check.sh uses `set -o pipefail`. Without the `|| true` fix,
# the grep|cut pipeline exits 1 when LOBSTER_DEV_MODE is absent, aborting the
# script immediately before any log file is written.
#
# We test two things:
#   a) LOBSTER_DEV_MODE=true  → exits 0 (suppressed, never reaches checks)
#   b) LOBSTER_DEV_MODE absent → script proceeds past the guard; we verify by
#      checking that the log file was created (a sign the script ran)
echo ""
echo -e "${BOLD}Scenario: daily-health-check.sh with LOBSTER_DEV_MODE absent${NC}"
FAKE_WORKSPACE="$TEST_TMPDIR/workspace"
FAKE_LOG_DIR="$FAKE_WORKSPACE/logs"
FAKE_MESSAGES_DIR="$FAKE_HOME/messages"
mkdir -p "$FAKE_LOG_DIR" "$FAKE_MESSAGES_DIR/inbox"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
# Minimal config — LOBSTER_DEV_MODE intentionally absent
TELEGRAM_BOT_TOKEN=dummy
EOF

# With LOBSTER_DEV_MODE absent, the script proceeds past the guard and runs checks.
# It will exit non-zero (many checks fail in this isolated env), but the log file
# is created only after the guard — proving the pipefail bug did not fire.
LOG_BEFORE=$(find "$FAKE_LOG_DIR" -name "daily-health-check.log" 2>/dev/null | wc -l)
env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
    LOBSTER_INSTALL_DIR="$FAKE_HOME/lobster" \
    LOBSTER_WORKSPACE="$FAKE_WORKSPACE" \
    LOBSTER_MESSAGES="$FAKE_MESSAGES_DIR" \
    bash "$REPO_ROOT/scripts/daily-health-check.sh" > /dev/null 2>&1 || true
LOG_AFTER=$(find "$FAKE_LOG_DIR" -name "daily-health-check.log" 2>/dev/null | wc -l)
if [ "$LOG_AFTER" -gt "$LOG_BEFORE" ]; then
    pass "daily-health-check.sh proceeds past guard (log written) with LOBSTER_DEV_MODE absent"
else
    fail "daily-health-check.sh proceeds past guard with LOBSTER_DEV_MODE absent" \
         "no log file created — script may have aborted early due to pipefail bug"
fi

echo ""
echo -e "${BOLD}Scenario: daily-health-check.sh with LOBSTER_DEV_MODE=true${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
LOBSTER_DEV_MODE=true
EOF

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_INSTALL_DIR="$FAKE_HOME/lobster" \
        LOBSTER_WORKSPACE="$FAKE_WORKSPACE" \
        LOBSTER_MESSAGES="$FAKE_MESSAGES_DIR" \
        bash "$REPO_ROOT/scripts/daily-health-check.sh" > /dev/null 2>&1; then
    pass "daily-health-check.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true"
else
    fail "daily-health-check.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true" "exited non-zero"
fi

# --- Test 6: periodic-self-check.sh ------------------------------------------
#
# periodic-self-check.sh uses `set -e`. It sources agent-status.sh from
# LOBSTER_INSTALL_DIR/scripts/. We provide a minimal stub that satisfies the
# source call so the script can run in isolation.
#
# Two scenarios:
#   a) LOBSTER_DEV_MODE=true  → exits 0 immediately (dev suppression)
#   b) LOBSTER_DEV_MODE absent → script passes the guard; exits 0 because
#      Guard 2 (no self-check in inbox) and Guard 3 (no pending agents) fire.

FAKE_LOBSTER_INSTALL="$TEST_TMPDIR/fake-lobster-install"
mkdir -p "$FAKE_LOBSTER_INSTALL/scripts"
# Minimal agent-status.sh stub: exports the two functions the script expects.
cat > "$FAKE_LOBSTER_INSTALL/scripts/agent-status.sh" <<'STUB'
scan_agent_status() { echo ""; }
scan_completed_tasks() { echo ""; }
STUB

echo ""
echo -e "${BOLD}Scenario: periodic-self-check.sh with LOBSTER_DEV_MODE absent${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
# Minimal config — LOBSTER_DEV_MODE intentionally absent
TELEGRAM_BOT_TOKEN=dummy
EOF

# The script exits 0 because Guard 2/3/4/5 all fire cleanly in the test env.
if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_MESSAGES="$FAKE_MESSAGES_DIR" \
        LOBSTER_INSTALL_DIR="$FAKE_LOBSTER_INSTALL" \
        bash "$REPO_ROOT/scripts/periodic-self-check.sh" > /dev/null 2>&1; then
    pass "periodic-self-check.sh exits 0 with LOBSTER_DEV_MODE absent"
else
    fail "periodic-self-check.sh exits 0 with LOBSTER_DEV_MODE absent" "exited non-zero"
fi

echo ""
echo -e "${BOLD}Scenario: periodic-self-check.sh with LOBSTER_DEV_MODE=true${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
LOBSTER_DEV_MODE=true
EOF

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_MESSAGES="$FAKE_MESSAGES_DIR" \
        LOBSTER_INSTALL_DIR="$FAKE_LOBSTER_INSTALL" \
        bash "$REPO_ROOT/scripts/periodic-self-check.sh" > /dev/null 2>&1; then
    pass "periodic-self-check.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true"
else
    fail "periodic-self-check.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true" "exited non-zero"
fi

# --- Test 7: dispatch-job.sh -------------------------------------------------
#
# dispatch-job.sh uses `set -e`. After the dev-mode guard, it checks whether the
# corresponding systemd timer is enabled; when no such timer exists, it exits 0
# silently. This gives us a clean exit-0 scenario in the test environment.
echo ""
echo -e "${BOLD}Scenario: dispatch-job.sh with LOBSTER_DEV_MODE absent${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
# Minimal config — LOBSTER_DEV_MODE intentionally absent
TELEGRAM_BOT_TOKEN=dummy
EOF

FAKE_JOB_WORKSPACE="$TEST_TMPDIR/workspace-dispatch"
mkdir -p "$FAKE_JOB_WORKSPACE/scheduled-jobs/logs" "$FAKE_JOB_WORKSPACE/scheduled-jobs/tasks"
# Pass a job name that has no matching systemd timer → script exits 0 (disabled)
if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_WORKSPACE="$FAKE_JOB_WORKSPACE" \
        LOBSTER_MESSAGES="$FAKE_MESSAGES_DIR" \
        LOBSTER_INSTALL_DIR="$FAKE_HOME/lobster" \
        bash "$REPO_ROOT/scheduled-tasks/dispatch-job.sh" "lobster-test-nonexistent-job-$$" > /dev/null 2>&1; then
    pass "dispatch-job.sh exits 0 with LOBSTER_DEV_MODE absent (timer not found)"
else
    fail "dispatch-job.sh exits 0 with LOBSTER_DEV_MODE absent (timer not found)" "exited non-zero"
fi

echo ""
echo -e "${BOLD}Scenario: dispatch-job.sh with LOBSTER_DEV_MODE=true${NC}"
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
LOBSTER_DEV_MODE=true
EOF

if env HOME="$FAKE_HOME" LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR" \
        LOBSTER_WORKSPACE="$FAKE_JOB_WORKSPACE" \
        LOBSTER_MESSAGES="$FAKE_MESSAGES_DIR" \
        LOBSTER_INSTALL_DIR="$FAKE_HOME/lobster" \
        bash "$REPO_ROOT/scheduled-tasks/dispatch-job.sh" "lobster-test-nonexistent-job-$$" > /dev/null 2>&1; then
    pass "dispatch-job.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true"
else
    fail "dispatch-job.sh exits 0 (suppressed) with LOBSTER_DEV_MODE=true" "exited non-zero"
fi

# --- Test 8: Verify post-reminder.sh actually writes the inbox file ----------
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
