#!/bin/bash
#===============================================================================
# Test: CLAUDECODE Environment Variable Leak Prevention
#
# Verifies that every script which launches `claude` (or creates a tmux session
# that will host claude) contains the `unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT` guard.
#
# Also verifies that systemd service files include `UnsetEnvironment=CLAUDECODE CLAUDE_CODE_ENTRYPOINT`.
#
# This test is structural (grep-based) — it catches regressions at CI time
# without needing to actually run Claude.
#
# Usage:  bash tests/test_claudecode_guard.sh
# Exit:   0 = all pass, 1 = failures found
#===============================================================================

set -euo pipefail

REPO_DIR="${LOBSTER_INSTALL_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PASS=0
FAIL=0
ERRORS=()

pass() {
    PASS=$((PASS + 1))
    echo "  PASS: $1"
}

fail() {
    FAIL=$((FAIL + 1))
    ERRORS+=("$1")
    echo "  FAIL: $1"
}

#===============================================================================
# Test 1: Scripts that directly invoke `claude` must have `unset CLAUDECODE`
#===============================================================================
echo ""
echo "=== Test 1: Scripts invoking 'claude' have 'unset CLAUDECODE' ==="

# Find all .sh files that invoke claude as a command (not just mentioning it
# in comments or echo statements). We look for lines where claude is invoked
# as a binary: `claude -p`, `claude --dangerously`, `exec.*claude`, etc.
# Exclude: .venv, node_modules, .git, comments-only mentions
SCRIPTS_INVOKING_CLAUDE=()

while IFS= read -r file; do
    # Skip .venv, node_modules, .git, test files (this very script)
    [[ "$file" == *".venv"* ]] && continue
    [[ "$file" == *"node_modules"* ]] && continue
    [[ "$file" == *".git/"* ]] && continue
    [[ "$file" == *"test_claudecode_guard"* ]] && continue

    # Check if the file has a non-comment line invoking claude as a command
    # (not just `claude mcp add`, which is config, not a session launch)
    # Match patterns: direct `claude -p`, `claude --dangerously`, `exec ... claude`,
    # `timeout ... claude -p`, `env -u CLAUDECODE claude`, and variable assignments
    # like `output=$(timeout 600 claude -p ...)`
    if grep -qE '(^|\$\()?\s*(claude\s+-p|claude\s+--dangerously|exec.*claude|timeout.*claude\s+-p|env\s+-u\s+CLAUDECODE\s+claude)' "$file" 2>/dev/null; then
        SCRIPTS_INVOKING_CLAUDE+=("$file")
    fi
done < <(find "$REPO_DIR" -name "*.sh" -type f 2>/dev/null)

if [[ ${#SCRIPTS_INVOKING_CLAUDE[@]} -eq 0 ]]; then
    fail "No scripts found invoking claude — detection logic is broken"
else
    echo "  Found ${#SCRIPTS_INVOKING_CLAUDE[@]} scripts invoking claude:"
    for script in "${SCRIPTS_INVOKING_CLAUDE[@]}"; do
        rel="${script#$REPO_DIR/}"
        echo "    - $rel"
    done
    echo ""

    for script in "${SCRIPTS_INVOKING_CLAUDE[@]}"; do
        rel="${script#$REPO_DIR/}"

        # Special case: scripts that use `env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude`
        # are already protected inline (e.g., token-refresh.sh). Accept that pattern too.
        if grep -q 'env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude' "$script" 2>/dev/null; then
            pass "$rel (uses env -u inline for both vars)"
            continue
        fi

        if grep -q 'unset CLAUDECODE' "$script" 2>/dev/null; then
            # Also verify CLAUDE_CODE_ENTRYPOINT is unset
            if grep -q 'CLAUDE_CODE_ENTRYPOINT' "$script" 2>/dev/null; then
                pass "$rel (both CLAUDECODE and CLAUDE_CODE_ENTRYPOINT)"
            else
                fail "$rel — unsets CLAUDECODE but missing CLAUDE_CODE_ENTRYPOINT"
            fi
        else
            fail "$rel — missing 'unset CLAUDECODE'"
        fi
    done
fi

#===============================================================================
# Test 2: Scripts that create tmux sessions for claude must clean tmux env
#===============================================================================
echo ""
echo "=== Test 2: Tmux-launching scripts clean tmux global environment ==="

# These scripts create tmux sessions where claude will run
TMUX_LAUNCHERS=()

while IFS= read -r file; do
    [[ "$file" == *".venv"* ]] && continue
    [[ "$file" == *"node_modules"* ]] && continue
    [[ "$file" == *".git/"* ]] && continue
    [[ "$file" == *"test_claudecode_guard"* ]] && continue

    # Look for tmux new-session commands (creating sessions, not just attaching)
    if grep -qE 'tmux.*new-session' "$file" 2>/dev/null; then
        TMUX_LAUNCHERS+=("$file")
    fi
done < <(find "$REPO_DIR" -name "*.sh" -type f 2>/dev/null)

for script in "${TMUX_LAUNCHERS[@]}"; do
    rel="${script#$REPO_DIR/}"

    # Must have either:
    # - tmux ... set-environment ... CLAUDECODE (cleaning tmux server env), OR
    # - unset CLAUDECODE (at minimum clearing shell env before tmux new-session)
    if grep -q 'set-environment.*CLAUDECODE' "$script" 2>/dev/null; then
        pass "$rel (cleans tmux env)"
    elif grep -q 'unset CLAUDECODE' "$script" 2>/dev/null; then
        pass "$rel (unsets before tmux)"
    else
        fail "$rel — creates tmux session but doesn't clean CLAUDECODE"
    fi
done

#===============================================================================
# Test 3: claude-persistent.sh (primary launcher) has full protection
#===============================================================================
echo ""
echo "=== Test 3: claude-persistent.sh has comprehensive CLAUDECODE protection ==="

PERSISTENT="$REPO_DIR/scripts/claude-persistent.sh"
if [[ ! -f "$PERSISTENT" ]]; then
    fail "claude-persistent.sh not found at $PERSISTENT"
else
    # Must unset CLAUDECODE
    if grep -q 'unset CLAUDECODE' "$PERSISTENT"; then
        pass "claude-persistent.sh unsets CLAUDECODE"
    else
        fail "claude-persistent.sh missing 'unset CLAUDECODE'"
    fi

    # Must also unset CLAUDE_CODE_ENTRYPOINT (the other leaked var)
    if grep -q 'CLAUDE_CODE_ENTRYPOINT' "$PERSISTENT"; then
        pass "claude-persistent.sh handles CLAUDE_CODE_ENTRYPOINT"
    else
        fail "claude-persistent.sh missing CLAUDE_CODE_ENTRYPOINT handling"
    fi

    # Must clean tmux server environment
    if grep -q 'set-environment.*CLAUDECODE' "$PERSISTENT"; then
        pass "claude-persistent.sh cleans tmux server env"
    else
        fail "claude-persistent.sh missing tmux set-environment cleanup"
    fi
fi

#===============================================================================
# Test 4: Systemd service files include UnsetEnvironment=CLAUDECODE
#===============================================================================
echo ""
echo "=== Test 4: Systemd service files include UnsetEnvironment=CLAUDECODE ==="

SERVICE_FILES=()
while IFS= read -r file; do
    # Only check service files whose ExecStart actually launches a claude session
    # (not router/transcription services that just mention "Claude" in Description)
    if grep -E '^ExecStart=.*(/claude|start-claude|claude-persistent|claude-wrapper)' "$file" >/dev/null 2>&1; then
        SERVICE_FILES+=("$file")
    # Also match templates with {{}} placeholders
    elif grep -E '^ExecStart=.*\{\{.*\}\}.*(start-claude|claude-persistent|claude-wrapper)' "$file" >/dev/null 2>&1; then
        SERVICE_FILES+=("$file")
    fi
done < <(find "$REPO_DIR/services" -name "*.service" -o -name "*.service.template" 2>/dev/null)

if [[ ${#SERVICE_FILES[@]} -eq 0 ]]; then
    fail "No claude-related service files found — detection logic is broken"
else
    for svc in "${SERVICE_FILES[@]}"; do
        rel="${svc#$REPO_DIR/}"
        if grep -q 'UnsetEnvironment=CLAUDECODE' "$svc"; then
            if grep -q 'CLAUDE_CODE_ENTRYPOINT' "$svc"; then
                pass "$rel (both CLAUDECODE and CLAUDE_CODE_ENTRYPOINT)"
            else
                fail "$rel — has UnsetEnvironment=CLAUDECODE but missing CLAUDE_CODE_ENTRYPOINT"
            fi
        else
            fail "$rel — missing 'UnsetEnvironment=CLAUDECODE'"
        fi
    done
fi

#===============================================================================
# Test 5: dispatch-job.sh does not invoke claude directly
#===============================================================================
echo ""
echo "=== Test 5: dispatch-job.sh does not invoke claude directly ==="

DISPATCH_JOB="$REPO_DIR/scheduled-tasks/dispatch-job.sh"
if [[ ! -f "$DISPATCH_JOB" ]]; then
    fail "dispatch-job.sh not found"
else
    if grep -q 'claude -p' "$DISPATCH_JOB" || grep -qP '(?<![/#"])claude\s+-' "$DISPATCH_JOB"; then
        fail "dispatch-job.sh invokes claude directly — must use inbox dispatch only"
    else
        pass "dispatch-job.sh does not invoke claude directly"
    fi
fi

#===============================================================================
# Test 6: Functional test — unset CLAUDECODE actually clears the variable
#===============================================================================
echo ""
echo "=== Test 6: Functional test — unset actually clears CLAUDECODE ==="

export CLAUDECODE=1
unset CLAUDECODE
if [[ -z "${CLAUDECODE:-}" ]]; then
    pass "unset CLAUDECODE clears the variable"
else
    fail "CLAUDECODE still set after unset (value: $CLAUDECODE)"
fi

#===============================================================================
# Test 7: Functional test — child process does NOT inherit after unset
#===============================================================================
echo ""
echo "=== Test 7: Child process does not inherit CLAUDECODE after unset ==="

export CLAUDECODE=1
unset CLAUDECODE
CHILD_VALUE=$(bash -c 'echo "${CLAUDECODE:-}"')
if [[ -z "$CHILD_VALUE" ]]; then
    pass "child process has no CLAUDECODE"
else
    fail "child process inherited CLAUDECODE=$CHILD_VALUE"
fi

#===============================================================================
# Test 8: Guard placement — unset comes BEFORE claude invocation
#===============================================================================
echo ""
echo "=== Test 8: Guard placement — unset comes before claude invocation ==="

for script in "${SCRIPTS_INVOKING_CLAUDE[@]}"; do
    rel="${script#$REPO_DIR/}"

    # Skip scripts that use env -u inline
    if grep -q 'env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude' "$script" 2>/dev/null; then
        pass "$rel (inline env -u, order N/A)"
        continue
    fi

    # Get line numbers
    unset_line=$(grep -n 'unset CLAUDECODE' "$script" 2>/dev/null | head -1 | cut -d: -f1)
    # Skip comment lines (starting with optional whitespace then #)
    claude_line=$(grep -nE '(^|\$\()?\s*(claude\s+-p|claude\s+--dangerously|exec.*claude|timeout.*claude)' "$script" 2>/dev/null | grep -vE '^[0-9]+:\s*#' | head -1 | cut -d: -f1 || true)

    if [[ -z "$unset_line" ]]; then
        fail "$rel — no unset CLAUDECODE found (already flagged)"
        continue
    fi

    if [[ -z "$claude_line" ]]; then
        # claude invocation might be in a heredoc or function — skip order check
        pass "$rel (claude invocation in heredoc/function, guard present)"
        continue
    fi

    if [[ "$unset_line" -lt "$claude_line" ]]; then
        pass "$rel (unset on L${unset_line}, claude on L${claude_line})"
    else
        fail "$rel — unset CLAUDECODE (L${unset_line}) comes AFTER claude invocation (L${claude_line})"
    fi
done

#===============================================================================
# Summary
#===============================================================================
echo ""
echo "==============================================="
echo "  Results: $PASS passed, $FAIL failed"
echo "==============================================="

if [[ $FAIL -gt 0 ]]; then
    echo ""
    echo "  Failures:"
    for err in "${ERRORS[@]}"; do
        echo "    - $err"
    done
    echo ""
    exit 1
fi

echo ""
echo "  All CLAUDECODE leak vectors are guarded."
exit 0
