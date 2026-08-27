#!/bin/bash
#===============================================================================
# Test Suite: Migration Runner (scripts/lib/migrations.sh)
#
# Verifies (issue #2200):
#   1. run_migrations() is idempotent and correctly applies a representative
#      migration (Migration 96: CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0) to a
#      fake config.env in isolation, without touching the real host.
#   2. install.sh actually sources scripts/lib/migrations.sh and calls
#      run_migrations() unconditionally — this is the actual bug fix: prior
#      to this change, install.sh never invoked the migration runner at all,
#      so a reimage-via-restore silently skipped every config.env migration.
#   3. upgrade.sh still sources the shared lib and calls run_migrations().
#
# Also verifies (issue #2246): run_migrations() runs all ~99 migrations
# unconditionally, and several of them shell out directly to the real
# `crontab` binary and to `sudo` (systemctl, usermod, tee) - commands that
# are NOT sandboxed by faking $LOBSTER_DIR/$WORKSPACE_DIR/etc, since
# `crontab -l`/`crontab -` always read/write the real per-user system
# crontab regardless of those variables. Running this test previously wrote
# real entries (built from the fake temp path) into the actual system
# crontab. This suite stubs `crontab` and `sudo` via a fake-bin directory
# prepended to $PATH (catches both direct shell calls and the
# subprocess.run(["sudo", ...]) calls made by the embedded Python migration
# block) and asserts the real system crontab is byte-for-byte unchanged
# after run_migrations() executes.
#
# Usage: bash tests/test-migrations-lib.sh
#        (run from repo root or any directory)
#===============================================================================

set -uo pipefail

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
LIB="$REPO_ROOT/scripts/lib/migrations.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-migrations-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

# Resolve the real `crontab` binary's absolute path BEFORE any $PATH
# stubbing happens below, and snapshot the real crontab's current contents.
# After run_migrations() executes (with the fake-bin stub shadowing
# `crontab` in $PATH), we re-snapshot via this same absolute path and
# assert byte-for-byte equality - proving the stub, not the real binary,
# absorbed every migration's crontab read/write (issue #2246).
REAL_CRONTAB_BIN="$(command -v crontab || true)"
if [ -n "$REAL_CRONTAB_BIN" ]; then
    REAL_CRONTAB_BEFORE="$("$REAL_CRONTAB_BIN" -l 2>/dev/null || true)"
fi

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); echo -e "  ${GREEN}PASS${NC} $1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); echo -e "  ${RED}FAIL${NC} $1"; echo -e "       ${YELLOW}$2${NC}"; }

assert_file_contains() {
    local label="$1" file="$2" pattern="$3"
    if grep -qF "$pattern" "$file" 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "expected '$file' to contain: $pattern"
    fi
}

assert_count() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$label"
    else
        fail "$label" "expected count: $expected / got: $actual"
    fi
}

echo ""
echo -e "${BOLD}Migration Runner Tests${NC}"
echo "lib: $LIB"
echo ""

#===============================================================================
# Group 1: run_migrations() behavior, in isolation
#===============================================================================
echo "-- run_migrations() applies and is idempotent --"

FAKE_LOBSTER_DIR="$TEST_TMPDIR/lobster"
FAKE_WORKSPACE_DIR="$TEST_TMPDIR/lobster-workspace"
FAKE_MESSAGES_DIR="$TEST_TMPDIR/messages"
FAKE_CONFIG_DIR="$TEST_TMPDIR/lobster-config"
FAKE_USER_CONFIG_DIR="$TEST_TMPDIR/lobster-user-config"
FAKE_CLAUDE_SETTINGS="$TEST_TMPDIR/settings.json"
FAKE_VENV_DIR="$TEST_TMPDIR/lobster/.venv"
mkdir -p "$FAKE_LOBSTER_DIR" "$FAKE_WORKSPACE_DIR" "$FAKE_MESSAGES_DIR/inbox" "$FAKE_CONFIG_DIR" "$FAKE_USER_CONFIG_DIR" "$FAKE_VENV_DIR/bin"

# Pre-existing config.env with everything except the migration-96 key,
# mirroring a host whose config.env predates issue #2142's fix.
cat > "$FAKE_CONFIG_DIR/config.env" <<'EOF'
TELEGRAM_BOT_TOKEN=fake-token
LOBSTER_ADMIN_CHAT_ID=12345
EOF

# Minimal jobs.json (issue #2246 follow-up): seeds a single enabled job with
# an absolute `command` path and a `schedule`, matching the exact shape
# Migration 55 (systemd-timer-from-jobs.json, scripts/lib/migrations.sh
# ~line 1055) requires to actually run its migration branch rather than be
# skipped because $WORKSPACE_DIR/scheduled-jobs/jobs.json doesn't exist. That
# branch is the one embedding the Python subprocess.run(["sudo", "tee", ...])
# call this suite's sudo stub needs to intercept and prove it caught.
mkdir -p "$FAKE_WORKSPACE_DIR/scheduled-jobs"
cat > "$FAKE_WORKSPACE_DIR/scheduled-jobs/jobs.json" <<'EOF'
{
  "jobs": {
    "test-follow-up-job": {
      "enabled": true,
      "command": "/usr/local/bin/lobster-test-follow-up-job.sh",
      "schedule": "*-*-* 04:00:00",
      "description": "Fake job fixture for migration test (issue #2246 follow-up)"
    }
  }
}
EOF

# Minimal logging/env contract required by scripts/lib/migrations.sh (see its
# header docstring) - install.sh's own stubs, reused here 1:1.
info()    { :; }
success() { :; }
warn()    { :; }
error()   { :; }
step()    { :; }
substep() { :; }
log_to_file() { :; }

# ---------------------------------------------------------------------------
# Command isolation (issue #2246): several migrations shell out directly to
# `crontab` and `sudo` (systemctl, usermod, tee) - real system binaries that
# are NOT sandboxed by the fake $LOBSTER_DIR/$WORKSPACE_DIR/etc above. A
# fake-bin directory prepended to $PATH intercepts these at the command
# resolution boundary, so it catches both plain shell invocations in
# migrations.sh AND the subprocess.run(["sudo", ...]) calls made by the
# embedded Python migration block (a bash function override would only
# catch the former).
FAKE_BIN_DIR="$TEST_TMPDIR/fake-bin"
FAKE_CRONTAB_STATE="$TEST_TMPDIR/fake-crontab-state"
mkdir -p "$FAKE_BIN_DIR"
: > "$FAKE_CRONTAB_STATE"

cat > "$FAKE_BIN_DIR/crontab" <<EOF
#!/bin/bash
# Fake crontab stub (tests/test-migrations-lib.sh, issue #2246) - never
# touches the real system crontab. Mimics the subset of \`crontab\` usage
# migrations.sh relies on: \`crontab -l\`, \`crontab -\` (write stdin),
# and \`crontab <file>\`.
#
# Several migrations chain \`crontab -l | grep ... | crontab -\` - bash
# starts every pipeline stage concurrently, so the read-side (\`-l\`) and
# write-side (\`-\`) invocations of this stub run at the same time. Writing
# via a plain \`cat > "\$STATE"\` truncates the state file at open time,
# racing the concurrent read-side's \`cat "\$STATE"\` and intermittently
# handing it a truncated/empty file. Write to a temp file and \`mv\` it into
# place instead (same technique the real crontab binary uses) so the state
# file is always replaced atomically: a concurrent reader sees either the
# complete old content or the complete new content, never a partial file.
STATE="$FAKE_CRONTAB_STATE"
case "\${1:-}" in
    -l)
        [ -s "\$STATE" ] && cat "\$STATE" || exit 1
        ;;
    -|"")
        TMP="\$STATE.tmp.\$\$"
        cat > "\$TMP" && mv "\$TMP" "\$STATE"
        ;;
    *)
        [ -f "\$1" ] && cp "\$1" "\$STATE" || exit 1
        ;;
esac
EOF
chmod +x "$FAKE_BIN_DIR/crontab"

FAKE_SUDO_TEE_LOG="$TEST_TMPDIR/fake-sudo-tee-log"
: > "$FAKE_SUDO_TEE_LOG"

cat > "$FAKE_BIN_DIR/sudo" <<EOF
#!/bin/bash
# Fake sudo stub (tests/test-migrations-lib.sh, issue #2246) - swallows all
# privileged calls (systemctl, usermod, cp, tee, ldconfig, ...) so tests
# never mutate the real host. \`sudo -n true\` (passwordless-sudo probe)
# succeeds; \`sudo tee ...\` consumes stdin so pipelines don't block.
#
# For \`tee\`, also record the target path into a log file (issue #2246
# follow-up) - this is how the test proves the embedded Python migration's
# subprocess.run(["sudo", "tee", ...]) call (Migration 55,
# systemd-timer-from-jobs.json) was actually caught by this stub, the same
# way the crontab stub's state file proves migrations 28/52 were caught.
TEE_LOG="$FAKE_SUDO_TEE_LOG"
if [ "\${1:-}" = "-n" ] && [ "\${2:-}" = "true" ]; then
    exit 0
fi
if [ "\${1:-}" = "tee" ]; then
    echo "\${2:-}" >> "\$TEE_LOG"
    cat >/dev/null
    exit 0
fi
exit 0
EOF
chmod +x "$FAKE_BIN_DIR/sudo"

export PATH="$FAKE_BIN_DIR:$PATH"

DRY_RUN=false
LOBSTER_DIR="$FAKE_LOBSTER_DIR"
WORKSPACE_DIR="$FAKE_WORKSPACE_DIR"
MESSAGES_DIR="$FAKE_MESSAGES_DIR"
LOBSTER_CONFIG_DIR="$FAKE_CONFIG_DIR"
USER_CONFIG_DIR="$FAKE_USER_CONFIG_DIR"
CONFIG_FILE="$FAKE_CONFIG_DIR/config.env"
CLAUDE_SETTINGS="$FAKE_CLAUDE_SETTINGS"
VENV_DIR="$FAKE_VENV_DIR"

# shellcheck source=../scripts/lib/migrations.sh
source "$LIB"

run_migrations
assert_file_contains "Migration 96 applies CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0 to config.env" \
    "$CONFIG_FILE" "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0"

run_migrations
occurrences=$(grep -c "^CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=" "$CONFIG_FILE")
assert_count "Migration 96 is idempotent (running twice does not duplicate the key)" "1" "$occurrences"

#===============================================================================
# Group 1b: crontab/sudo isolation (issue #2246)
#===============================================================================
echo ""
echo "-- run_migrations() never touches the real system crontab --"

# Sanity check: the crontab-writing migrations actually ran and exercised
# the stub (proves this is a meaningful regression test, not a no-op because
# the crontab code paths were skipped for some unrelated reason).
assert_file_contains "Migration 28 (LOBSTER-LOG-EXPORT) wrote to the fake crontab stub" \
    "$FAKE_CRONTAB_STATE" "LOBSTER-LOG-EXPORT"
assert_file_contains "Migration 52 (LOBSTER-GHOST-DETECTOR) wrote to the fake crontab stub" \
    "$FAKE_CRONTAB_STATE" "LOBSTER-GHOST-DETECTOR"
assert_file_contains "Migration 55 (systemd-timer-from-jobs.json) exercised the sudo tee stub" \
    "$FAKE_SUDO_TEE_LOG" "/etc/systemd/system/lobster-test-follow-up-job.timer"

if [ -n "$REAL_CRONTAB_BIN" ]; then
    REAL_CRONTAB_AFTER="$("$REAL_CRONTAB_BIN" -l 2>/dev/null || true)"
    if [ "$REAL_CRONTAB_BEFORE" = "$REAL_CRONTAB_AFTER" ]; then
        pass "Real system crontab is byte-for-byte unchanged after run_migrations()"
    else
        fail "Real system crontab is byte-for-byte unchanged after run_migrations()" \
            "real crontab changed during the test run - the crontab stub did not intercept every call"
    fi
else
    pass "Real system crontab is byte-for-byte unchanged after run_migrations() (skipped: no crontab binary on this host)"
fi

#===============================================================================
# Group 2: install.sh actually wires the runner in (the real bug fix)
#===============================================================================
echo ""
echo "-- install.sh calls run_migrations() unconditionally --"

INSTALL_SH="$REPO_ROOT/install.sh"
UPGRADE_SH="$REPO_ROOT/scripts/upgrade.sh"

assert_file_contains "install.sh sources scripts/lib/migrations.sh" \
    "$INSTALL_SH" 'source "${INSTALL_DIR}/scripts/lib/migrations.sh"'
assert_file_contains "install.sh calls run_migrations" \
    "$INSTALL_SH" "run_migrations"
assert_file_contains "upgrade.sh sources scripts/lib/migrations.sh" \
    "$UPGRADE_SH" 'source "$LOBSTER_DIR/scripts/lib/migrations.sh"'
assert_file_contains "upgrade.sh calls run_migrations" \
    "$UPGRADE_SH" "run_migrations"

# Guard against regressing back to a duplicated/local definition in either
# caller - the whole point of #2200's fix is a single shared implementation.
if grep -q "^run_migrations() {" "$INSTALL_SH" 2>/dev/null; then
    fail "install.sh does not redefine run_migrations() locally" "found a local definition - should source the shared lib instead"
else
    pass "install.sh does not redefine run_migrations() locally"
fi
if grep -q "^run_migrations() {" "$UPGRADE_SH" 2>/dev/null; then
    fail "upgrade.sh does not redefine run_migrations() locally" "found a local definition - should source the shared lib instead"
else
    pass "upgrade.sh does not redefine run_migrations() locally"
fi

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
