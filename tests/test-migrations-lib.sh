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

# Minimal logging/env contract required by scripts/lib/migrations.sh (see its
# header docstring) - install.sh's own stubs, reused here 1:1.
info()    { :; }
success() { :; }
warn()    { :; }
error()   { :; }
step()    { :; }
substep() { :; }
log_to_file() { :; }

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
