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
"""

from __future__ import annotations

# The agent_type value written when the dispatcher registers its own session
# (see session_start(agent_type=...) in src/agents/session_store.py and the
# SessionStart hook's crash-restart registration path). Must match
# DISPATCHER_AGENT_TYPE in scripts/lib/agent_sessions.sh.
DISPATCHER_AGENT_TYPE = "dispatcher"

# SQL WHERE-clause fragment excluding dispatcher rows from a query over
# agent_sessions. COALESCE guards against agent_type being NULL (legacy rows
# predating the column, or subagents that never set it) — NULL != 'dispatcher'
# is NULL (falsy) in SQL, so without COALESCE those rows would be silently
# excluded from results that should include them.
#
# Must match DISPATCHER_EXCLUSION_SQL in scripts/lib/agent_sessions.sh.
DISPATCHER_EXCLUSION_SQL = f"COALESCE(agent_type, '') != '{DISPATCHER_AGENT_TYPE}'"


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

    Convenience wrapper around is_dispatcher_agent_type() for callers holding
    a full session dict (e.g. from get_active_sessions() or a row returned by
    get_unnotified_completed()) rather than a bare agent_type string.
    """
    return is_dispatcher_agent_type(session.get("agent_type"))
