"""
Tests for src/utils/agent_types.py — the shared dispatcher-exclusion predicate
(Linear BIS-723).

Before this module existed, `agent_type != 'dispatcher'` was reimplemented
independently at five call sites and had to be fixed four separate times
(issue #781, PR #2099, PR #2103). These tests pin down the one shared
definition so a future accidental removal or inversion of the check is
caught here first.
"""

from src.utils.agent_types import (
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
