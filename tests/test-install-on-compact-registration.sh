#!/bin/bash
#===============================================================================
# Test Suite: install.sh setup_claude_hooks() registers on-compact (issue #2285)
#
# setup_claude_hooks() used to decide whether on-compact was already wired with
#     .hooks.SessionStart[]? | select(.matcher == "compact")
# which is satisfied by ANY SessionStart entry on matcher "compact" — for
# example the inject-bootup-context compact entry the same function registers
# right after it. On a settings.json that already had that entry but had lost
# on-compact, on-compact was silently never registered. It was also registered
# on matcher "compact", which is unreliable (issue #1947) and which Migration 88
# and install.sh's later top-level block both replace with "".
#
# The function is extracted from install.sh verbatim and run against fixture
# settings files: it only reads/writes $CLAUDE_SETTINGS with jq and chmods hook
# paths under a fake $INSTALL_DIR, so it runs safely in isolation.
#
# Cases covered:
#   1. Settings with only an inject-bootup-context compact entry → on-compact
#      gets registered (before the fix: it did not)
#   2. Empty settings → on-compact registered exactly once, matcher ""
#   3. Re-running is idempotent (still exactly one on-compact entry)
#   4. A legacy on-compact entry on matcher "compact" is not duplicated
#
# Usage: bash tests/test-install-on-compact-registration.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

# The tmp dir name must not contain "on-compact": the registration checks match
# on command substrings, and fixture paths live under this dir.
TEST_TMPDIR=$(mktemp -d /tmp/lobster-install-hooks-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected '$expected', got '$actual'"; fi
}

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available on this host"
    exit 0
fi

# install.sh logging helpers used inside the function.
info()    { :; }
success() { :; }
warn()    { :; }

# Extract setup_claude_hooks() from install.sh (from its definition line to the
# first column-0 closing brace outside a heredoc) and define it in this shell.
FUNC_SRC=$(awk '
    /^setup_claude_hooks\(\) \{/ { f = 1 }
    !f { next }
    { print }
    heredoc != "" { if ($0 == heredoc) heredoc = ""; next }
    match($0, /<< *\047?[A-Za-z_]+\047?/) {
        heredoc = substr($0, RSTART, RLENGTH); gsub(/[< \047]/, "", heredoc); next
    }
    /^\}/ { exit }
' "$INSTALL_SH")
if [[ -z "$FUNC_SRC" ]]; then
    echo -e "${RED}FAIL${NC} could not extract setup_claude_hooks() from install.sh"
    exit 1
fi
eval "$FUNC_SRC"

INSTALL_DIR="$TEST_TMPDIR/lobster"
CLAUDE_SETTINGS_DIR="$TEST_TMPDIR/claude"
CLAUDE_SETTINGS="$CLAUDE_SETTINGS_DIR/settings.json"
MESSAGES_DIR="$TEST_TMPDIR/messages"  # interpolated into some hook commands
mkdir -p "$INSTALL_DIR/hooks" "$CLAUDE_SETTINGS_DIR"

on_compact_count() {
    jq '[.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-compact"))] | length' "$CLAUDE_SETTINGS"
}
on_compact_matchers() {
    jq -r '[.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-compact")) | .matcher] | join(",")' "$CLAUDE_SETTINGS"
}

echo ""
echo "install.sh setup_claude_hooks(): on-compact registration"
echo ""

# --- Case 1 -------------------------------------------------------------------
cat > "$CLAUDE_SETTINGS" <<EOF
{
  "hooks": {
    "SessionStart": [
      {"matcher": "compact", "hooks": [{"type": "command", "command": "python3 $INSTALL_DIR/hooks/inject-bootup-context.py", "timeout": 10}]}
    ]
  }
}
EOF
setup_claude_hooks >/dev/null 2>&1
begin_test "on-compact registered when only an inject-bootup-context compact entry exists"
assert_eq "$(on_compact_count)" "1"

# --- Case 2 -------------------------------------------------------------------
rm -f "$CLAUDE_SETTINGS"
setup_claude_hooks >/dev/null 2>&1
begin_test "fresh settings: on-compact registered once with matcher \"\""
assert_eq "$(on_compact_count):$(on_compact_matchers)" "1:"

# --- Case 3 -------------------------------------------------------------------
setup_claude_hooks >/dev/null 2>&1
begin_test "re-running setup_claude_hooks does not duplicate on-compact"
assert_eq "$(on_compact_count)" "1"

# --- Case 4 -------------------------------------------------------------------
cat > "$CLAUDE_SETTINGS" <<EOF
{
  "hooks": {
    "SessionStart": [
      {"matcher": "compact", "hooks": [{"type": "command", "command": "python3 $INSTALL_DIR/hooks/on-compact.py", "timeout": 30}]}
    ]
  }
}
EOF
setup_claude_hooks >/dev/null 2>&1
begin_test "legacy matcher=\"compact\" on-compact entry is not duplicated"
assert_eq "$(on_compact_count)" "1"

echo ""
echo "Results: $PASS/$TOTAL passed"
if [[ "$FAIL" -gt 0 ]]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
