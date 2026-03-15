"""
Pending Agents Tracker — thin adapter over session_store

This module provides a backward-compatible public API that delegates all
storage to the SQLite-backed session_store module. The previous implementation
used a JSON file (~/messages/config/pending-agents.json) with file locking;
the new implementation uses WAL-mode SQLite for reliability and queryability.

Public API (unchanged from v1):
    add_pending_agent(agent_id, description, chat_id, task_id, source,
                      output_file, timeout_minutes, path) -> None
    remove_pending_agent(agent_id, path) -> None
    get_pending_agents(path) -> list
    is_agent_pending(agent_id, path) -> bool
    pending_agent_count(path) -> int

Backward-compatibility notes:
  - The `path` parameter is accepted and passed through to session_store, which
    uses it as the SQLite DB path (not a JSON path). Tests that pass a path
    override receive an isolated DB — previously, passing a path pointed to an
    alternate JSON file. The semantics are preserved for test isolation.
  - All callers in inbox_server.py, tracker.py tests, and CLAUDE.md use the
    same parameter names — no changes needed at call sites.
  - The `path` default of None resolves to the standard DB location in
    session_store (~/messages/config/agent_sessions.db).
"""

from pathlib import Path

from agents.session_store import (
    session_start,
    session_end,
    get_active_sessions,
    find_session,
    init_db,
)


# =============================================================================
# Public API — identical signatures to the JSON-based v1 implementation
# =============================================================================


def add_pending_agent(
    agent_id: str,
    description: str,
    chat_id: int,
    task_id: str | None = None,
    source: str = "telegram",
    output_file: str | None = None,
    timeout_minutes: int | None = None,
    trigger_message_id: str | None = None,
    trigger_snippet: str | None = None,
    path: Path | None = None,
) -> None:
    """Record a newly-spawned background agent.

    Delegates to session_store.session_start(). Idempotent for the same
    agent_id — duplicate calls replace the existing entry.

    Args:
        agent_id:           Unique identifier for the agent.
        description:        Human-readable summary of what the agent is doing.
        chat_id:            Chat/channel to notify when the agent completes.
        task_id:            Logical task identifier passed to write_result.
        source:             Messaging platform ('telegram', 'slack', etc.).
        output_file:        Full path to the Claude Code agent output file in /tmp.
        timeout_minutes:    Expected maximum runtime.
        trigger_message_id: Inbox message_id that caused this spawn (causality).
        trigger_snippet:    First 200 chars of the triggering message text (PII).
        path:               DB path override (for testing). Accepts and ignores the
                            old JSON-path semantics — treated as SQLite DB path.
    """
    session_start(
        id=agent_id,
        description=description,
        chat_id=str(chat_id),
        task_id=task_id,
        source=source,
        output_file=output_file,
        timeout_minutes=timeout_minutes,
        trigger_message_id=trigger_message_id,
        trigger_snippet=trigger_snippet,
        path=path,
    )


def remove_pending_agent(
    agent_id: str,
    path: Path | None = None,
) -> None:
    """Remove a completed agent from the pending list.

    Delegates to session_store.session_end() with status='completed'.
    Idempotent: removing a non-existent agent_id is a no-op.

    Args:
        agent_id: The ID (or task_id) to remove.
        path:     DB path override (for testing).
    """
    session_end(id_or_task_id=agent_id, status="completed", path=path)


def get_pending_agents(path: Path | None = None) -> list:
    """Return a snapshot of all currently running agent sessions.

    Returns:
        List of session dicts from session_store.get_active_sessions().
        Each dict includes: id, description, chat_id, status, spawned_at,
        elapsed_seconds, and optional task_id, output_file, timeout_minutes.
    """
    return get_active_sessions(path=path)


def is_agent_pending(agent_id: str, path: Path | None = None) -> bool:
    """Return True if the given agent_id is registered and still running.

    Args:
        agent_id: The ID to check (matches on id or task_id).
        path:     DB path override (for testing).
    """
    session = find_session(agent_id, path=path)
    return session is not None and session.get("status") == "running"


def pending_agent_count(path: Path | None = None) -> int:
    """Return the number of currently running agent sessions."""
    return len(get_active_sessions(path=path))
