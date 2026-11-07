#!/usr/bin/env python3
"""PreToolUse hook: block wait_for_messages / check_inbox for subagent sessions.

## Background (issue #2323)

A background agent spawned for a one-shot scheduled job (task_id
`scheduled-x-bookmarks-20261107`) completed its work and correctly called
`write_result`/`write_task_output` — but then, instead of terminating, called
`wait_for_messages()` itself and ran as a second rogue dispatcher loop for
~75 minutes, consuming messages from the shared inbox meant for the primary
dispatcher. This caused a duplicate-processing incident on an unrelated user
request.

`hooks/session_role.py` already names the two tools that must be
dispatcher-exclusive in its `DISPATCHER_ONLY_TOOLS` constant, but — before this
hook — nothing actually enforced that at the Claude Code tool-call layer. (The
MCP server has its own session-tagging guard for these tools; this hook is a
second, independent layer of defense that blocks the call before it ever
reaches the server, and keeps working even if the server-side guard's state
ever drifts.)

## Detection: agent_id presence

Claude Code injects an `agent_id` field into PreToolUse hook payloads only for
subagent (Task-tool-spawned) sessions — never for the dispatcher's own tool
calls. This is the same fast-path signal already used by
`session_role.is_dispatcher_session()` and `require-background-agent.py`
("Subagents are exempt" check), chosen here over the startup-flag-based
`session_role.is_dispatcher()` because that flag is deleted by
`inject-bootup-context.py` immediately after SessionStart and is therefore
unusable for the entire remainder of either session's lifetime — exactly the
window in which a subagent calling wait_for_messages matters.

Unlike `require-write-result.py`'s SubagentStop enforcement (which only fires
when a session tries to *stop*), this hook fires on every tool call — so a
subagent that avoids stopping by looping on wait_for_messages is still caught,
which is the exact failure mode in #2323.

## Scope

Blocks unconditionally for any subagent session, regardless of whether
write_result has already been called. A subagent has no legitimate reason to
ever call these two tools — not just after reporting its result.
"""
import json
import sys

# Tools that only the Lobster dispatcher may call. Mirrors
# session_role.DISPATCHER_ONLY_TOOLS (kept here as a literal tuple so this
# hook has no import-time dependency on session_role's module path setup).
_BLOCKED_TOOLS = frozenset({
    "mcp__lobster-inbox__wait_for_messages",
    "mcp__lobster-inbox__check_inbox",
})


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # If we can't read input, don't block.

    tool_name = data.get("tool_name", "")
    if tool_name not in _BLOCKED_TOOLS:
        sys.exit(0)

    # agent_id is present only in subagent hook payloads (never for the
    # dispatcher's own tool calls). Absent -> this is the dispatcher; allow.
    if not data.get("agent_id"):
        sys.exit(0)

    print(
        f"BLOCKED: '{tool_name}' is dispatcher-exclusive. This session is a "
        "subagent (agent_id present), not the Lobster dispatcher.\n\n"
        "If you have finished your assigned task, call write_result (or "
        "write_task_output) and then stop — do not call wait_for_messages or "
        "check_inbox. Calling either of these from a subagent risks consuming "
        "messages from the shared inbox meant for the primary dispatcher loop "
        "(see issue #2323).",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
