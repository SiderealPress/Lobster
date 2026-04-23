#!/bin/bash
#===============================================================================
# Test Suite: Template Generator (scripts/lib/template.sh)
#
# Verifies that _tmpl_generate_from_template() correctly substitutes all 8
# {{PLACEHOLDER}} variables and fails on missing or unresolved placeholders.
#
# Usage: bash tests/test-template-generator.sh
#        (run from repo root or any directory)
#===============================================================================

set -euo pipefail

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

# Locate lib relative to this test file
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/template.sh"

# Test isolation
TEST_TMPDIR=$(mktemp -d /tmp/lobster-test-template-XXXXXX)
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

assert_equals() {
    local label="$1"
    local expected="$2"
    local actual="$3"
    if [ "$expected" = "$actual" ]; then
        pass "$label"
    else
        fail "$label" "expected: $expected / got: $actual"
    fi
}

assert_file_not_contains() {
    local label="$1"
    local file="$2"
    local pattern="$3"
    if grep -q "$pattern" "$file" 2>/dev/null; then
        fail "$label" "file still contains: $pattern"
    else
        pass "$label"
    fi
}

assert_file_exists() {
    local label="$1"
    local file="$2"
    if [ -f "$file" ]; then
        pass "$label"
    else
        fail "$label" "file does not exist: $file"
    fi
}

assert_file_not_exists() {
    local label="$1"
    local file="$2"
    if [ ! -f "$file" ]; then
        pass "$label"
    else
        fail "$label" "file should not exist: $file"
    fi
}

# --- Setup -------------------------------------------------------------------

echo ""
echo -e "${BOLD}Template Generator Tests${NC}"
echo "lib: $LIB"
echo ""

# Source the library under test
# shellcheck source=../scripts/lib/template.sh
source "$LIB"

# Set canonical LOBSTER_* test values (all 8 placeholders)
export LOBSTER_USER="testuser"
export LOBSTER_GROUP="testgroup"
export LOBSTER_HOME="/home/testuser"
export LOBSTER_INSTALL_DIR="/opt/lobster"
export LOBSTER_WORKSPACE="/opt/lobster-workspace"
export LOBSTER_MESSAGES="/opt/messages"
export LOBSTER_CONFIG_DIR="/etc/lobster"
export LOBSTER_USER_CONFIG="/home/testuser/lobster-user-config"

# --- Fixture: template with all 8 placeholders -------------------------------

ALL_PLACEHOLDERS_TEMPLATE="$TEST_TMPDIR/all-placeholders.service.template"
cat > "$ALL_PLACEHOLDERS_TEMPLATE" <<'TMPL'
[Unit]
Description=Test service

[Service]
User={{USER}}
Group={{GROUP}}
Environment=HOME={{HOME}}
Environment=INSTALL_DIR={{INSTALL_DIR}}
Environment=WORKSPACE={{WORKSPACE_DIR}}
Environment=MESSAGES={{MESSAGES_DIR}}
Environment=CONFIG={{CONFIG_DIR}}
Environment=USER_CONFIG={{USER_CONFIG_DIR}}
WorkingDirectory={{INSTALL_DIR}}
ExecStart={{INSTALL_DIR}}/bin/start

[Install]
WantedBy=multi-user.target
TMPL

# --- Tests -------------------------------------------------------------------

echo "Substitution correctness:"

OUTPUT="$TEST_TMPDIR/all-placeholders.service"
_tmpl_generate_from_template "$ALL_PLACEHOLDERS_TEMPLATE" "$OUTPUT"
assert_file_exists "output file created" "$OUTPUT"

assert_equals "{{USER}} substituted"          "testuser"                          "$(grep '^User=' "$OUTPUT" | cut -d= -f2)"
assert_equals "{{GROUP}} substituted"         "testgroup"                         "$(grep '^Group=' "$OUTPUT" | cut -d= -f2)"
assert_equals "{{HOME}} substituted"          "Environment=HOME=/home/testuser"   "$(grep '^Environment=HOME=' "$OUTPUT")"
assert_equals "{{INSTALL_DIR}} substituted"   "Environment=INSTALL_DIR=/opt/lobster" "$(grep '^Environment=INSTALL_DIR=' "$OUTPUT")"
assert_equals "{{WORKSPACE_DIR}} substituted" "Environment=WORKSPACE=/opt/lobster-workspace" "$(grep '^Environment=WORKSPACE=' "$OUTPUT")"
assert_equals "{{MESSAGES_DIR}} substituted"  "Environment=MESSAGES=/opt/messages" "$(grep '^Environment=MESSAGES=' "$OUTPUT")"
assert_equals "{{CONFIG_DIR}} substituted"    "Environment=CONFIG=/etc/lobster"   "$(grep '^Environment=CONFIG=' "$OUTPUT")"
assert_equals "{{USER_CONFIG_DIR}} substituted" "Environment=USER_CONFIG=/home/testuser/lobster-user-config" "$(grep '^Environment=USER_CONFIG=' "$OUTPUT")"

echo ""
echo "No unresolved placeholders:"
assert_file_not_contains "no {{ remaining in output" "$OUTPUT" '{{'

echo ""
echo "Error cases:"

# Missing template file
MISSING_OUTPUT="$TEST_TMPDIR/missing-output.service"
if _tmpl_generate_from_template "$TEST_TMPDIR/nonexistent.template" "$MISSING_OUTPUT" 2>/dev/null; then
    fail "missing template returns error" "expected non-zero exit"
else
    pass "missing template returns error"
fi
assert_file_not_exists "no output file on missing template" "$MISSING_OUTPUT"

# Unresolved placeholder (unset var scenario — temporarily unset one)
PARTIAL_TEMPLATE="$TEST_TMPDIR/partial.service.template"
echo "ExecStart={{INSTALL_DIR}}/start {{UNKNOWN_PLACEHOLDER}}" > "$PARTIAL_TEMPLATE"
PARTIAL_OUTPUT="$TEST_TMPDIR/partial.service"
# Inject an unresolvable placeholder directly into the template content; the
# sed expression won't match it, leaving {{ in the output.
if _tmpl_generate_from_template "$PARTIAL_TEMPLATE" "$PARTIAL_OUTPUT" 2>/dev/null; then
    fail "unresolved placeholder returns error" "expected non-zero exit"
else
    pass "unresolved placeholder returns error"
fi
assert_file_not_exists "output cleaned up on unresolved placeholder" "$PARTIAL_OUTPUT"

# Idempotency: running twice produces same result
_tmpl_generate_from_template "$ALL_PLACEHOLDERS_TEMPLATE" "$OUTPUT"
_tmpl_generate_from_template "$ALL_PLACEHOLDERS_TEMPLATE" "$OUTPUT"
assert_file_not_contains "idempotent: still no {{ after second run" "$OUTPUT" '{{'

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
