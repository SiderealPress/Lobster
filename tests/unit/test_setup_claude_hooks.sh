#!/usr/bin/env bash
# test_setup_claude_hooks.sh
#
# Unit tests for the setup_claude_hooks() function in install.sh.
#
# Verifies that:
#   1. setup_claude_hooks creates settings.json when it does not exist
#   2. All expected hooks are registered (idempotent check: running twice is safe)
#   3. Permissions bypass is applied
#   4. Previously-missing hooks (block-claude-p, dispatcher-state-*, etc.) are present
#
# Run: bash tests/unit/test_setup_claude_hooks.sh
# Requires: bash, jq

set -euo pipefail

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

# Set up a temp environment that install.sh will use
TMPDIR_BASE=$(mktemp -d)
export HOME="$TMPDIR_BASE/home"
export INSTALL_DIR="$TMPDIR_BASE/lobster"
export MESSAGES_DIR="$TMPDIR_BASE/messages"
mkdir -p "$HOME/.claude" "$INSTALL_DIR/hooks" "$MESSAGES_DIR/config"

# Create stub hook files so chmod succeeds
HOOKS=(
    no-auto-memory link-checker require-subagent-type require-background-agent
    require-task-id-in-prompt dispatcher-inline-tool-guard system-file-protect
    secret-scanner block-claude-p require-register-agent-task-id pre-tool-heartbeat
    dispatcher-state-pretool post-compact-gate catchup-gate restore-exec-bit
    auto-register-agent context-monitor dispatcher-state-posttool thinking-heartbeat
    write-dispatcher-session-id inject-bootup-context on-compact inject-debug-bootup
    on-fresh-start require-wait-for-messages dispatcher-state-stop require-write-result
    require-auditor-context-update
)
for h in "${HOOKS[@]}"; do
    touch "$INSTALL_DIR/hooks/${h}.py"
done

# Extract and run setup_claude_hooks from install.sh.
# We use python3 to reliably extract the full function (handles heredocs correctly).
INSTALL_SH="$(cd "$(dirname "$0")/../.." && pwd)/install.sh"

FUNC_BODY=$(python3 - "$INSTALL_SH" << 'PYEOF'
import sys, re

with open(sys.argv[1]) as f:
    lines = f.readlines()

start = None
depth = 0
in_heredoc = False
heredoc_end = None

for i, line in enumerate(lines):
    stripped = line.rstrip()
    if start is None:
        if stripped == 'setup_claude_hooks() {':
            start = i
            depth = 1
        continue
    if in_heredoc:
        if stripped == heredoc_end:
            in_heredoc = False
        continue
    m = re.search(r"<<\s*'?(\w+)'?", line)
    if m and not in_heredoc:
        heredoc_end = m.group(1)
        in_heredoc = True
        continue
    depth += stripped.count('{') - stripped.count('}')
    if depth == 0:
        print(''.join(lines[start:i+1]), end='')
        break
PYEOF
)

if [ -z "$FUNC_BODY" ]; then
    echo "FATAL: Could not extract setup_claude_hooks() from $INSTALL_SH"
    exit 1
fi
# Stub logging helpers that setup_claude_hooks depends on
info()    { :; }
success() { :; }
step()    { :; }
warn()    { echo "WARN: $*" >&2; }

eval "$FUNC_BODY"

# Run setup_claude_hooks once
setup_claude_hooks

SETTINGS="$HOME/.claude/settings.json"

# Test 1: settings.json exists
if [ -f "$SETTINGS" ]; then
    pass "settings.json created"
else
    fail "settings.json not created"
fi

# Test 2: permissions bypass
if jq -e '.permissions.defaultMode == "bypassPermissions"' "$SETTINGS" > /dev/null 2>&1; then
    pass "permissions bypass set"
else
    fail "permissions bypass missing"
fi

check_hook() {
    local desc="$1"
    local jq_query="$2"
    if jq -e "$jq_query" "$SETTINGS" > /dev/null 2>&1; then
        pass "$desc"
    else
        fail "$desc"
    fi
}

# Test 3: PreToolUse hooks
check_hook "no-auto-memory (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.matcher == "Write|Edit")'
check_hook "link-checker (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.matcher == "mcp__lobster-inbox__send_reply")'
check_hook "require-subagent-type (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.matcher == "Agent") | select(.hooks[]?.command | contains("require-subagent-type"))'
check_hook "require-background-agent (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("require-background-agent"))'
check_hook "require-task-id-in-prompt (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("require-task-id-in-prompt"))'
check_hook "dispatcher-inline-tool-guard (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("dispatcher-inline-tool-guard"))'
check_hook "system-file-protect (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("system-file-protect"))'
check_hook "secret-scanner (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("secret-scanner"))'
check_hook "block-claude-p (PreToolUse) [previously missing from install.sh]" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("block-claude-p"))'
check_hook "require-register-agent-task-id (PreToolUse) [previously missing from install.sh]" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("require-register-agent-task-id"))'
check_hook "pre-tool-heartbeat (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("pre-tool-heartbeat"))'
check_hook "dispatcher-state-pretool (PreToolUse) [previously missing from install.sh]" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("dispatcher-state-pretool"))'
check_hook "post-compact-gate (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("post-compact-gate"))'
check_hook "catchup-gate (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("catchup-gate"))'

# Test 4: PostToolUse hooks
check_hook "restore-exec-bit (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.matcher == "Edit|Write")'
check_hook "auto-register-agent (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | test("auto-register-agent"))'
check_hook "context-monitor (PostToolUse) with Bash matcher" \
    '.hooks.PostToolUse[]? | select(.matcher == "Bash|mcp__lobster-inbox__|Agent")'
check_hook "dispatcher-state-posttool (PostToolUse) [previously missing from install.sh]" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("dispatcher-state-posttool"))'
check_hook "thinking-heartbeat (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("thinking-heartbeat"))'

# Test 5: SessionStart hooks
check_hook "write-dispatcher-session-id (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("write-dispatcher-session-id"))'
check_hook "inject-bootup-context all-sessions (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "")'
check_hook "on-compact (SessionStart)" \
    '.hooks.SessionStart[]? | select(.matcher == "compact") | select(.hooks[]?.command | contains("on-compact"))'
check_hook "inject-bootup-context compact (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "compact")'
check_hook "inject-debug-bootup (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-debug-bootup"))'
check_hook "on-fresh-start (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-fresh-start"))'

# Test 6: Stop hooks
check_hook "require-wait-for-messages (Stop)" \
    '.hooks.Stop[]? | select(.hooks[]?.command | contains("require-wait-for-messages"))'
check_hook "dispatcher-state-stop (Stop) [previously missing from install.sh]" \
    '.hooks.Stop[]? | select(.hooks[]?.command | contains("dispatcher-state-stop"))'

# Test 7: SubagentStop hooks
check_hook "require-write-result (SubagentStop)" \
    '.hooks.SubagentStop[]? | select(.hooks[]?.command | contains("require-write-result"))'
check_hook "require-auditor-context-update (SubagentStop)" \
    '.hooks.SubagentStop[]? | select(.hooks[]?.command | contains("require-auditor-context-update"))'

# Test 8: Idempotency — run again and count hooks (no duplicates)
setup_claude_hooks
PRETOOL_COUNT=$(jq '[.hooks.PreToolUse[]?] | length' "$SETTINGS")
POSTTOOL_COUNT=$(jq '[.hooks.PostToolUse[]?] | length' "$SETTINGS")
SESSION_COUNT=$(jq '[.hooks.SessionStart[]?] | length' "$SETTINGS")
STOP_COUNT=$(jq '[.hooks.Stop[]?] | length' "$SETTINGS")
SUBAGENT_COUNT=$(jq '[.hooks.SubagentStop[]?] | length' "$SETTINGS")

# Run a third time
setup_claude_hooks
PRETOOL_COUNT2=$(jq '[.hooks.PreToolUse[]?] | length' "$SETTINGS")

if [ "$PRETOOL_COUNT" -eq "$PRETOOL_COUNT2" ]; then
    pass "idempotent: no duplicate PreToolUse hooks after second run"
else
    fail "idempotent check failed: PreToolUse count went from $PRETOOL_COUNT to $PRETOOL_COUNT2 on re-run"
fi

# Cleanup
rm -rf "$TMPDIR_BASE"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
