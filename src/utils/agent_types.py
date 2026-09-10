"""
Dispatcher-exclusion: single source of truth (Linear BIS-723).

The dispatcher's own row in agent_sessions.db must never be mistaken for a
subagent that needs cleanup, dead-agent notification, or "pending agent"
counting. Before this module existed, the check `agent_type != 'dispatcher'`
was reimplemented independently at five call sites (session_store.py x2,
inbox_server.py x2, periodic-self-check.sh) and had to be fixed four separate
times (issue #781, PR #2099, PR #2103) because there was no shared definition
to update. This module is that shared definition for all Python call sites.

Cross-referenced with scripts/lib/agent_sessions.sh, which holds the
equivalent SQL fragment for shell callers (periodic-self-check.sh) that query
agent_sessions.db directly via `sqlite3` rather than importing this module.
Python and bash cannot literally share one function, so the two files carry
matching comments pointing at each other — if one changes, the other must
change too.

## Belt-and-suspenders `id` fallback (issue #2226)

`agent_type` is an optional parameter on the `session_start` MCP tool — a
dispatcher bootup call that omits it (e.g. `session_start(agent_id=
"lobster-dispatcher", description=..., chat_id=...)` with no `agent_type=
"dispatcher"`) leaves the row's `agent_type` column NULL. `COALESCE(agent_type,
'') != 'dispatcher'` then evaluates to `'' != 'dispatcher'` → TRUE, so the row
is *not* excluded — it gets miscounted as a pending/dead subagent on every
query. This exact failure mode produced a permanent false-positive
`[1 agents pending]` self-check every 3 minutes (issue #2226).

The fix mirrors the belt-and-suspenders pattern `scripts/agent-monitor.py`'s
`_is_dispatcher_agent()` already used for its own call site (issue #2176):
exclude by the static `agent_id` the dispatcher always registers under
(`lobster-dispatcher`, per `.claude/sys.dispatcher.bootup.md`'s bootup
instructions) in addition to `agent_type`, so a row is recognized as the
dispatcher's own session even when `agent_type` failed to land.
"""

from __future__ import annotations

# The agent_type value written when the dispatcher registers its own session
# (see session_start(agent_type=...) in src/agents/session_store.py and the
# SessionStart hook's crash-restart registration path). Must match
# DISPATCHER_AGENT_TYPE in scripts/lib/agent_sessions.sh.
DISPATCHER_AGENT_TYPE = "dispatcher"

# The static agent_id the dispatcher always registers itself under (see
# .claude/sys.dispatcher.bootup.md's session_start(agent_id="lobster-dispatcher",
# ...) bootup instruction, and scripts/agent-monitor.py's _DISPATCHER_AGENT_ID,
# which now imports this constant rather than hand-copying the literal).
# Belt-and-suspenders identifier for when agent_type itself is NULL (issue
# #2226). Must match DISPATCHER_AGENT_ID in scripts/lib/agent_sessions.sh.
DISPATCHER_AGENT_ID = "lobster-dispatcher"

# SQL WHERE-clause fragment excluding dispatcher rows from a query over
# agent_sessions. COALESCE guards against agent_type being NULL (legacy rows
# predating the column, or subagents that never set it) — NULL != 'dispatcher'
# is NULL (falsy) in SQL, so without COALESCE those rows would be silently
# excluded from results that should include them. The `id !=` clause is the
# belt-and-suspenders fallback (issue #2226): it excludes the dispatcher's row
# by its static agent_id even on the (otherwise unguarded) path where
# agent_type itself landed NULL.
#
# Must match DISPATCHER_EXCLUSION_SQL in scripts/lib/agent_sessions.sh.
DISPATCHER_EXCLUSION_SQL = (
    f"(COALESCE(agent_type, '') != '{DISPATCHER_AGENT_TYPE}' "
    f"AND id != '{DISPATCHER_AGENT_ID}')"
)


def is_dispatcher_agent_type(agent_type: str | None) -> bool:
    """Return True if agent_type identifies the dispatcher's own session.

    Exact, case-sensitive match against DISPATCHER_AGENT_TYPE. None and the
    empty string are never the dispatcher — those represent ghost/legacy rows
    with no agent_type recorded, which are handled by separate guards (see
    tests/unit/test_mcp_server/test_ghost_session_fixes.py).
    """
    return (agent_type or "") == DISPATCHER_AGENT_TYPE


def is_dispatcher_row(session: dict) -> bool:
    """Return True if a session dict represents the dispatcher's own session.

    Checks agent_type first (the structural signal). Falls back to the static
    agent_id ('lobster-dispatcher') so the row is still recognized as the
    dispatcher's own session even if agent_type landed NULL — e.g. because the
    dispatcher's own `session_start` bootup call omitted the optional
    `agent_type` parameter (issue #2226). Mirrors the same agent_id fallback
    `scripts/agent-monitor.py`'s `_is_dispatcher_agent()` already applies.
    """
    return (
        is_dispatcher_agent_type(session.get("agent_type"))
        or session.get("id") == DISPATCHER_AGENT_ID
    )
