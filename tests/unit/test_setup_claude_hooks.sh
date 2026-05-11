#!/usr/bin/env bash
# test_setup_claude_hooks.sh
#
# Validates the Claude Code hook configuration in ~/.claude/settings.json.
#
# Verifies that:
#   1. settings.json exists and is valid JSON
#   2. Permissions bypass is configured
#   3. Required hooks are registered
#   4. on-compact.py uses matcher="" (not matcher="compact") — issue #1947
#   5. No redundant compact-matcher inject-bootup-context entry exists
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

SETTINGS="$HOME/.claude/settings.json"

# Test 0: settings.json exists and is valid JSON
if [ ! -f "$SETTINGS" ]; then
    echo "FATAL: $SETTINGS not found"
    exit 1
fi
if ! jq empty "$SETTINGS" 2>/dev/null; then
    echo "FATAL: $SETTINGS is not valid JSON"
    exit 1
fi
pass "settings.json exists and is valid JSON"

# Test 1: permissions bypass
if jq -e '.permissions.defaultMode == "bypassPermissions"' "$SETTINGS" > /dev/null 2>&1; then
    pass "permissions.defaultMode = bypassPermissions"
else
    fail "permissions.defaultMode missing or not bypassPermissions"
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

# Test 2: PreToolUse hooks
check_hook "no-auto-memory (PreToolUse, matcher=Write|Edit)" \
    '.hooks.PreToolUse[]? | select(.matcher == "Write|Edit") | select(.hooks[]?.command | contains("no-auto-memory"))'
check_hook "link-checker (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("link-checker"))'
check_hook "require-subagent-type (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("require-subagent-type"))'
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
check_hook "block-claude-p (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("block-claude-p"))'
check_hook "require-register-agent-task-id (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("require-register-agent-task-id"))'
check_hook "pre-tool-heartbeat (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("pre-tool-heartbeat"))'
check_hook "dispatcher-state-pretool (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("dispatcher-state-pretool"))'
check_hook "post-compact-gate (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("post-compact-gate"))'
check_hook "catchup-gate (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | test("catchup-gate"))'

# Test 3: PostToolUse hooks
check_hook "restore-exec-bit (PostToolUse, matcher=Edit|Write)" \
    '.hooks.PostToolUse[]? | select(.matcher == "Edit|Write") | select(.hooks[]?.command | contains("restore-exec-bit"))'
check_hook "auto-register-agent (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | test("auto-register-agent"))'
check_hook "context-monitor (PostToolUse, matcher=Bash|mcp__lobster-inbox__.*|Agent)" \
    '.hooks.PostToolUse[]? | select(.matcher == "Bash|mcp__lobster-inbox__.*|Agent")'
check_hook "dispatcher-state-posttool (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("dispatcher-state-posttool"))'
check_hook "thinking-heartbeat (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("thinking-heartbeat"))'

# Test 4: SessionStart hooks
check_hook "inject-bootup-context (SessionStart, matcher='')" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "")'
check_hook "inject-debug-bootup (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-debug-bootup"))'
check_hook "on-fresh-start (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-fresh-start"))'

# Test 5: on-compact.py hook correctness (issue #1947)
# on-compact.py must use matcher="" — the script has a self-gate that reads
# hook_event_name and exits early unless it's a compact event.
# matcher="compact" is unreliable in CC 2.1.119 (~37% fire rate since April 17).
check_hook "on-compact.py present in SessionStart" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-compact"))'
check_hook "on-compact.py uses matcher='' (issue #1947: compact matcher unreliable in CC 2.1.119)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-compact")) | select(.matcher == "")'
# Regression guard: on-compact.py must NOT use matcher="compact"
if jq -e '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("on-compact")) | select(.matcher == "compact")' "$SETTINGS" > /dev/null 2>&1; then
    fail "REGRESSION: on-compact.py uses matcher='compact' (unreliable in CC 2.1.119 — see issue #1947)"
else
    pass "on-compact.py does not use matcher='compact' (regression guard)"
fi

# Test 6: No redundant compact-matcher inject-bootup-context entry
# The empty-matcher entry already fires on all session types (startup, resume,
# compact). A second compact-matcher entry would cause double-injection on
# every session type. Verify it doesn't exist.
if jq -e '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "compact")' "$SETTINGS" > /dev/null 2>&1; then
    fail "inject-bootup-context has a compact-matcher entry (causes double-injection — empty-matcher covers all session types)"
else
    pass "no redundant compact-matcher inject-bootup-context entry"
fi

# Test 7: Stop hooks
check_hook "require-wait-for-messages (Stop)" \
    '.hooks.Stop[]? | select(.hooks[]?.command | contains("require-wait-for-messages"))'
check_hook "dispatcher-state-stop (Stop)" \
    '.hooks.Stop[]? | select(.hooks[]?.command | contains("dispatcher-state-stop"))'

# Test 8: SubagentStop hooks
check_hook "require-write-result (SubagentStop)" \
    '.hooks.SubagentStop[]? | select(.hooks[]?.command | contains("require-write-result"))'
check_hook "require-auditor-context-update (SubagentStop)" \
    '.hooks.SubagentStop[]? | select(.hooks[]?.command | contains("require-auditor-context-update"))'

# ---------------------------------------------------------------------------
# Unit tests: setup_claude_hooks() function in isolation (PR #1952)
# ---------------------------------------------------------------------------

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

# Test 9: settings.json exists (unit — created by setup_claude_hooks)
if [ -f "$SETTINGS" ]; then
    pass "setup_claude_hooks creates settings.json"
else
    fail "setup_claude_hooks did not create settings.json"
fi

# Test 10: permissions bypass (unit)
if jq -e '.permissions.defaultMode == "bypassPermissions"' "$SETTINGS" > /dev/null 2>&1; then
    pass "setup_claude_hooks sets permissions bypass"
else
    fail "setup_claude_hooks: permissions bypass missing"
fi

# Test 11: PreToolUse hooks (unit — checks against generated settings)
check_hook "setup_claude_hooks: no-auto-memory (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.matcher == "Write|Edit")'
check_hook "setup_claude_hooks: link-checker (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.matcher == "mcp__lobster-inbox__send_reply")'
check_hook "setup_claude_hooks: require-subagent-type (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.matcher == "Agent") | select(.hooks[]?.command | contains("require-subagent-type"))'
check_hook "setup_claude_hooks: block-claude-p (PreToolUse) [previously missing from install.sh]" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("block-claude-p"))'
check_hook "setup_claude_hooks: require-register-agent-task-id (PreToolUse) [previously missing from install.sh]" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("require-register-agent-task-id"))'
check_hook "setup_claude_hooks: pre-tool-heartbeat (PreToolUse)" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("pre-tool-heartbeat"))'
check_hook "setup_claude_hooks: dispatcher-state-pretool (PreToolUse) [previously missing from install.sh]" \
    '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("dispatcher-state-pretool"))'

# Test 12: PostToolUse hooks (unit)
check_hook "setup_claude_hooks: restore-exec-bit (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.matcher == "Edit|Write")'
check_hook "setup_claude_hooks: auto-register-agent (PostToolUse)" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | test("auto-register-agent"))'
check_hook "setup_claude_hooks: context-monitor (PostToolUse) with Bash matcher" \
    '.hooks.PostToolUse[]? | select(.matcher == "Bash|mcp__lobster-inbox__.*|Agent")'
check_hook "setup_claude_hooks: dispatcher-state-posttool (PostToolUse) [previously missing from install.sh]" \
    '.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("dispatcher-state-posttool"))'

# Test 13: SessionStart hooks (unit)
check_hook "setup_claude_hooks: inject-bootup-context all-sessions (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "")'
check_hook "setup_claude_hooks: on-compact (SessionStart)" \
    '.hooks.SessionStart[]? | select(.matcher == "compact") | select(.hooks[]?.command | contains("on-compact"))'
check_hook "setup_claude_hooks: inject-bootup-context compact (SessionStart)" \
    '.hooks.SessionStart[]? | select(.hooks[]?.command | contains("inject-bootup-context")) | select(.matcher == "compact")'

# Test 14: Stop hooks (unit)
check_hook "setup_claude_hooks: require-wait-for-messages (Stop)" \
    '.hooks.Stop[]? | select(.hooks[]?.command | contains("require-wait-for-messages"))'
check_hook "setup_claude_hooks: dispatcher-state-stop (Stop) [previously missing from install.sh]" \
    '.hooks.Stop[]? | select(.hooks[]?.command | contains("dispatcher-state-stop"))'

# Test 15: SubagentStop hooks (unit)
check_hook "setup_claude_hooks: require-write-result (SubagentStop)" \
    '.hooks.SubagentStop[]? | select(.hooks[]?.command | contains("require-write-result"))'
check_hook "setup_claude_hooks: require-auditor-context-update (SubagentStop)" \
    '.hooks.SubagentStop[]? | select(.hooks[]?.command | contains("require-auditor-context-update"))'

# Test 16: Idempotency — run again and count hooks (no duplicates)
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
