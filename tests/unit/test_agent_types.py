"""
Tests for src/utils/agent_types.py — the shared dispatcher-exclusion predicate
(Linear BIS-723).

Before this module existed, `agent_type != 'dispatcher'` was reimplemented
independently at five call sites and had to be fixed four separate times
(issue #781, PR #2099, PR #2103). These tests pin down the one shared
definition so a future accidental removal or inversion of the check is
caught here first.
"""

import sqlite3

from src.utils.agent_types import (
    DISPATCHER_AGENT_ID,
    DISPATCHER_AGENT_TYPE,
    DISPATCHER_EXCLUSION_SQL,
    is_dispatcher_agent_type,
    is_dispatcher_row,
)


def test_dispatcher_agent_type_is_excluded():
    """The exact dispatcher agent_type value must be recognized as the dispatcher."""
    assert is_dispatcher_agent_type(DISPATCHER_AGENT_TYPE) is True


def test_real_subagent_type_is_not_excluded():
    assert is_dispatcher_agent_type("subagent") is False
    assert is_dispatcher_agent_type("functional-engineer") is False


def test_none_agent_type_is_not_excluded():
    """Ghost/legacy rows with no agent_type recorded are NOT the dispatcher.

    These are handled by separate guards (see test_ghost_session_fixes.py) —
    is_dispatcher_agent_type only matches the exact dispatcher marker.
    """
    assert is_dispatcher_agent_type(None) is False


def test_empty_string_agent_type_is_not_excluded():
    assert is_dispatcher_agent_type("") is False


def test_match_is_case_sensitive():
    assert is_dispatcher_agent_type("DISPATCHER") is False
    assert is_dispatcher_agent_type("Dispatcher") is False


def test_is_dispatcher_row_reads_agent_type_field():
    assert is_dispatcher_row({"id": "abc", "agent_type": "dispatcher"}) is True
    assert is_dispatcher_row({"id": "abc", "agent_type": "subagent"}) is False
    assert is_dispatcher_row({"id": "abc"}) is False


def test_dispatcher_exclusion_sql_matches_constant():
    """The SQL fragment must reference the same value as DISPATCHER_AGENT_TYPE.

    Guards against the SQL string constant and the Python predicate drifting
    apart (e.g. someone editing one without the other).
    """
    assert f"'{DISPATCHER_AGENT_TYPE}'" in DISPATCHER_EXCLUSION_SQL
    assert "COALESCE(agent_type, '')" in DISPATCHER_EXCLUSION_SQL
    assert "!=" in DISPATCHER_EXCLUSION_SQL


# ---------------------------------------------------------------------------
# Issue #2226: NULL agent_type on the dispatcher's own row (agent_id fallback)
#
# session_start()'s agent_type parameter is optional. When the dispatcher's
# own bootup call omits it, the row's agent_type column lands NULL.
# COALESCE(agent_type, '') != 'dispatcher' then evaluates to '' != 'dispatcher'
# -> TRUE, so the row was NOT excluded pre-fix — it got counted as a pending
# subagent by periodic-self-check.sh (and any other DISPATCHER_EXCLUSION_SQL
# caller) on every query, forever, producing a permanent false-positive
# "[1 agents pending]" self-check every 3 minutes with zero real subagents
# running. These tests reproduce that exact row shape and confirm the id
# fallback now excludes it.
# ---------------------------------------------------------------------------

def _dispatcher_row_with_null_agent_type() -> dict:
    """The exact row shape reported in issue #2226's live reproduction."""
    return {
        "id": DISPATCHER_AGENT_ID,
        "agent_type": None,
        "status": "running",
        "chat_id": "6645894734",
        "description": "Lobster dispatcher main loop",
    }


def test_is_dispatcher_row_excludes_null_agent_type_via_id_fallback():
    """The bug reproduction: is_dispatcher_row() must return True for the
    dispatcher's row even when agent_type is NULL, via the id fallback."""
    assert is_dispatcher_row(_dispatcher_row_with_null_agent_type()) is True


def test_is_dispatcher_row_id_fallback_does_not_swallow_real_null_type_subagent():
    """A real subagent with a NULL/missing agent_type (legacy row, or a
    subagent type that was never set) must still NOT be treated as the
    dispatcher — the id fallback only fires for the exact static agent_id."""
    assert is_dispatcher_row({"id": "real-subagent-001", "agent_type": None}) is False


def _make_agent_sessions_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE agent_sessions (
            id TEXT PRIMARY KEY,
            agent_type TEXT,
            status TEXT NOT NULL DEFAULT 'running'
        )
        """
    )


def _pending_count(conn: sqlite3.Connection) -> int:
    """Mirror periodic-self-check.sh's PENDING_COUNT query exactly."""
    cursor = conn.execute(
        f"""
        SELECT COUNT(*) FROM agent_sessions
        WHERE status IN ('running','starting') AND {DISPATCHER_EXCLUSION_SQL}
        """
    )
    return cursor.fetchone()[0]


def test_pending_count_sql_excludes_dispatcher_row_with_null_agent_type():
    """Reproduces the false-positive count reported live in issue #2226
    directly against the shared SQL fragment (not just the Python predicate),
    since periodic-self-check.sh queries agent_sessions.db with this exact
    fragment via raw sqlite3, bypassing is_dispatcher_row() entirely.

    Pre-fix, this row (matching the live DB dump in the issue) would be
    counted, producing PENDING_COUNT=1 with zero real subagents running.
    """
    conn = sqlite3.connect(":memory:")
    _make_agent_sessions_db(conn)
    conn.execute(
        "INSERT INTO agent_sessions (id, agent_type, status) VALUES (?, NULL, 'running')",
        (DISPATCHER_AGENT_ID,),
    )
    conn.commit()

    assert _pending_count(conn) == 0


def test_pending_count_sql_still_counts_real_subagent_alongside_null_dispatcher_row():
    """The fix must not over-exclude: a real pending subagent must still be
    counted even while the dispatcher's NULL-agent_type row is present."""
    conn = sqlite3.connect(":memory:")
    _make_agent_sessions_db(conn)
    conn.execute(
        "INSERT INTO agent_sessions (id, agent_type, status) VALUES (?, NULL, 'running')",
        (DISPATCHER_AGENT_ID,),
    )
    conn.execute(
        "INSERT INTO agent_sessions (id, agent_type, status) VALUES ('real-subagent-001', 'subagent', 'running')"
    )
    conn.commit()

    assert _pending_count(conn) == 1
