#!/bin/bash
#===============================================================================
# Test script for private configuration overlay functionality
#
# This script tests the overlay mechanism without running a full installation.
# It sources the functions from install.sh and tests them directly.
#===============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Test state
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Test logging
test_pass() { echo -e "${GREEN}[PASS]${NC} $1"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
test_fail() { echo -e "${RED}[FAIL]${NC} $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }
test_skip() { echo -e "${YELLOW}[SKIP]${NC} $1"; }

run_test() {
    local name="$1"
    TESTS_RUN=$((TESTS_RUN + 1))
    echo ""
    echo "Running: $name"
}

#===============================================================================
# Setup
#===============================================================================

echo "=========================================="
echo "  Hyperion Overlay Test Suite"
echo "=========================================="

# Find script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Create temporary directories
TEST_DIR=$(mktemp -d)
INSTALL_DIR="$TEST_DIR/hyperion"
WORKSPACE_DIR="$TEST_DIR/workspace"
MESSAGES_DIR="$TEST_DIR/messages"
PRIVATE_CONFIG_DIR="$TEST_DIR/private-config"

mkdir -p "$INSTALL_DIR/config"
mkdir -p "$INSTALL_DIR/.claude/agents"
mkdir -p "$INSTALL_DIR/scheduled-tasks/tasks"
mkdir -p "$WORKSPACE_DIR"
mkdir -p "$MESSAGES_DIR"

# Stub logging functions
info() { echo "[INFO] $1"; }
success() { echo "[OK] $1"; }
warn() { echo "[WARN] $1"; }
error() { echo "[ERROR] $1"; }
step() { echo "▶ $1"; }

#===============================================================================
# Source overlay functions from install.sh
#===============================================================================

# Extract and source just the overlay functions
extract_functions() {
    local install_sh="$REPO_DIR/install.sh"

    # Source the function definitions
    eval "$(sed -n '/^apply_private_overlay()/,/^}/p' "$install_sh")"
    eval "$(sed -n '/^run_hook()/,/^}/p' "$install_sh")"
}

extract_functions

#===============================================================================
# Test 1: No private config dir set
#===============================================================================

run_test "No HYPERION_CONFIG_DIR set"
unset HYPERION_CONFIG_DIR
output=$(apply_private_overlay 2>&1)
if echo "$output" | grep -q "No private config directory"; then
    test_pass "Correctly reports no config dir"
else
    test_fail "Should report no config dir"
fi

#===============================================================================
# Test 2: Non-existent private config dir
#===============================================================================

run_test "Non-existent private config dir"
HYPERION_CONFIG_DIR="/nonexistent/path"
output=$(apply_private_overlay 2>&1)
if echo "$output" | grep -q "not found"; then
    test_pass "Correctly warns about missing dir"
else
    test_fail "Should warn about missing dir"
fi

#===============================================================================
# Test 3: Overlay config.env
#===============================================================================

run_test "Overlay config.env"
mkdir -p "$PRIVATE_CONFIG_DIR"
echo "TEST_VAR=test_value" > "$PRIVATE_CONFIG_DIR/config.env"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
apply_private_overlay >/dev/null 2>&1

if [ -f "$INSTALL_DIR/config/config.env" ]; then
    if grep -q "TEST_VAR=test_value" "$INSTALL_DIR/config/config.env"; then
        test_pass "config.env overlaid correctly"
    else
        test_fail "config.env content mismatch"
    fi
else
    test_fail "config.env not copied"
fi

#===============================================================================
# Test 4: Overlay CLAUDE.md
#===============================================================================

run_test "Overlay CLAUDE.md"
echo "# Custom Claude Instructions" > "$PRIVATE_CONFIG_DIR/CLAUDE.md"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
apply_private_overlay >/dev/null 2>&1

if [ -f "$WORKSPACE_DIR/CLAUDE.md" ]; then
    if grep -q "Custom Claude Instructions" "$WORKSPACE_DIR/CLAUDE.md"; then
        test_pass "CLAUDE.md overlaid correctly"
    else
        test_fail "CLAUDE.md content mismatch"
    fi
else
    test_fail "CLAUDE.md not copied"
fi

#===============================================================================
# Test 5: Overlay agents
#===============================================================================

run_test "Overlay agents directory"
mkdir -p "$PRIVATE_CONFIG_DIR/agents"
echo "# Custom Agent" > "$PRIVATE_CONFIG_DIR/agents/custom-agent.md"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
apply_private_overlay >/dev/null 2>&1

if [ -f "$INSTALL_DIR/.claude/agents/custom-agent.md" ]; then
    test_pass "Agent file overlaid correctly"
else
    test_fail "Agent file not copied"
fi

#===============================================================================
# Test 6: Overlay scheduled-tasks
#===============================================================================

run_test "Overlay scheduled-tasks directory"
mkdir -p "$PRIVATE_CONFIG_DIR/scheduled-tasks"
echo "# Morning task" > "$PRIVATE_CONFIG_DIR/scheduled-tasks/morning.md"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
apply_private_overlay >/dev/null 2>&1

if [ -f "$INSTALL_DIR/scheduled-tasks/morning.md" ]; then
    test_pass "Scheduled task overlaid correctly"
else
    test_fail "Scheduled task not copied"
fi

#===============================================================================
# Test 7: Run hook - no config dir
#===============================================================================

run_test "Run hook with no config dir"
unset HYPERION_CONFIG_DIR
run_hook "post-install.sh"
test_pass "Hook silently skipped when no config dir"

#===============================================================================
# Test 8: Run hook - hook doesn't exist
#===============================================================================

run_test "Run hook when hook file doesn't exist"
mkdir -p "$PRIVATE_CONFIG_DIR/hooks"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
run_hook "nonexistent-hook.sh" 2>&1
test_pass "Hook silently skipped when file doesn't exist"

#===============================================================================
# Test 9: Run hook - hook not executable
#===============================================================================

run_test "Run hook when hook is not executable"
echo '#!/bin/bash' > "$PRIVATE_CONFIG_DIR/hooks/post-install.sh"
echo 'echo "Hook ran"' >> "$PRIVATE_CONFIG_DIR/hooks/post-install.sh"
# Don't make it executable
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
output=$(run_hook "post-install.sh" 2>&1)
if echo "$output" | grep -q "not executable"; then
    test_pass "Correctly warns about non-executable hook"
else
    test_fail "Should warn about non-executable hook"
fi

#===============================================================================
# Test 10: Run hook successfully
#===============================================================================

run_test "Run hook successfully"
chmod +x "$PRIVATE_CONFIG_DIR/hooks/post-install.sh"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
output=$(run_hook "post-install.sh" 2>&1)
if echo "$output" | grep -q "Hook completed"; then
    test_pass "Hook executed successfully"
else
    test_fail "Hook did not complete successfully"
fi

#===============================================================================
# Test 11: Hook receives environment variables
#===============================================================================

run_test "Hook receives environment variables"
cat > "$PRIVATE_CONFIG_DIR/hooks/env-test.sh" << 'EOF'
#!/bin/bash
if [ -n "$HYPERION_INSTALL_DIR" ] && [ -n "$HYPERION_WORKSPACE_DIR" ] && [ -n "$HYPERION_MESSAGES_DIR" ]; then
    exit 0
else
    exit 1
fi
EOF
chmod +x "$PRIVATE_CONFIG_DIR/hooks/env-test.sh"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
output=$(run_hook "env-test.sh" 2>&1)
if echo "$output" | grep -q "Hook completed"; then
    test_pass "Hook received environment variables"
else
    test_fail "Hook did not receive environment variables"
fi

#===============================================================================
# Test 12: Hook failure reports correct exit code
#===============================================================================

run_test "Hook failure reports correct exit code"
cat > "$PRIVATE_CONFIG_DIR/hooks/failing-hook.sh" << 'EOF'
#!/bin/bash
exit 42
EOF
chmod +x "$PRIVATE_CONFIG_DIR/hooks/failing-hook.sh"
HYPERION_CONFIG_DIR="$PRIVATE_CONFIG_DIR"
output=$(run_hook "failing-hook.sh" 2>&1)
if echo "$output" | grep -q "exit code: 42"; then
    test_pass "Hook failure reports correct exit code"
else
    test_fail "Hook failure should report exit code 42, got: $output"
fi

#===============================================================================
# Cleanup
#===============================================================================

rm -rf "$TEST_DIR"

#===============================================================================
# Summary
#===============================================================================

echo ""
echo "=========================================="
echo "  Test Summary"
echo "=========================================="
echo ""
echo "Total tests: $TESTS_RUN"
echo -e "Passed:      ${GREEN}$TESTS_PASSED${NC}"
echo -e "Failed:      ${RED}$TESTS_FAILED${NC}"
echo ""

if [ "$TESTS_FAILED" -gt 0 ]; then
    exit 1
else
    echo "All tests passed!"
    exit 0
fi
