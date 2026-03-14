"""
Pending Agents Tracker

Manages a JSON file that records background agents spawned by Lobster so
that the dispatcher can relay results to Drew even after a context reset.

File location: ~/messages/config/pending-agents.json
Format:
    {
      "agents": [
        {
          "id": "abc123",
          "task_id": "fix-all-problems-1741967040",
          "description": "Implement feature X on issue #42",
          "chat_id": 1234567890,
          "source": "telegram",
          "started_at": "2026-02-22T10:30:00.000000Z",
          "output_file": "/tmp/claude-1000/.../tasks/abc123.output",
          "timeout_minutes": 30,
          "status": "running"
        }
      ]
    }

Design principles:
- Pure functions where possible; side effects only in add/remove
- Atomic writes via write-to-temp-then-rename (POSIX guarantee)
- File locking to handle concurrent access safely
- Graceful degradation: missing file treated as empty agent list
"""

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Default path: ~/messages/config/pending-agents.json
_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_DEFAULT_PATH = _MESSAGES_DIR / "config" / "pending-agents.json"


# =============================================================================
# Pure helpers
# =============================================================================

def _empty_store() -> dict:
    """Return a fresh empty agents store."""
    return {"agents": []}


def _make_agent_entry(
    agent_id: str,
    description: str,
    chat_id: int,
    task_id: str | None = None,
    source: str = "telegram",
    output_file: str | None = None,
    timeout_minutes: int | None = None,
    status: str = "running",
) -> dict:
    """Construct an immutable agent record.

    Args:
        agent_id:        Unique identifier extracted from the Task tool result.
        description:     Human-readable summary of what the agent is doing.
        chat_id:         Chat/channel to notify when the agent completes.
        task_id:         Logical task identifier (e.g. 'fix-all-problems-1741967040').
                         Typically matches the task_id the subagent will pass to write_result.
        source:          Messaging platform ('telegram', 'slack', etc.).
        output_file:     Full path to the Claude Code agent output file in /tmp.
                         Used for liveness detection: stat the file mtime to determine
                         if the agent is still active.
        timeout_minutes: Expected maximum runtime. Agents older than this without
                         output file activity can be presumed dead.
        status:          Lifecycle state — 'running' (default) | 'dead' | 'completed'.
    """
    entry: dict[str, Any] = {
        "id": agent_id,
        "description": description,
        "chat_id": chat_id,
        "source": source,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
    if task_id is not None:
        entry["task_id"] = task_id
    if output_file is not None:
        entry["output_file"] = output_file
    if timeout_minutes is not None:
        entry["timeout_minutes"] = timeout_minutes
    return entry


def _filter_out(agents: list, lookup_key: str) -> list:
    """Return agents list with the matching entry removed (pure, non-mutating).

    Matches on either the 'id' field or the 'task_id' field so that callers
    using either identifier (e.g. handle_write_result passes task_id as the
    lookup key) correctly remove the record.
    """
    return [
        a for a in agents
        if a.get("id") != lookup_key and a.get("task_id") != lookup_key
    ]


def _find_agent(agents: list, agent_id: str) -> dict | None:
    """Return the first agent matching agent_id, or None."""
    matches = [a for a in agents if a.get("id") == agent_id]
    return matches[0] if matches else None


# =============================================================================
# File I/O
# =============================================================================

def _read_store(path: Path) -> dict:
    """Read and parse the pending-agents JSON file.

    Returns an empty store if the file is missing or malformed.
    Never raises.
    """
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        # Validate expected shape; fall back to empty on unexpected structure
        if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
            return _empty_store()
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_store()


def _atomic_write(path: Path, data: dict) -> None:
    """Atomically write data to path as pretty-printed JSON.

    Uses write-to-temp-then-rename. On POSIX, rename() within the same
    filesystem is atomic, so readers never observe a partial file.

    Raises:
        OSError: If the write or rename fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _with_lock(path: Path, fn):
    """Execute fn(store: dict) -> dict under a file lock, then write result.

    Uses a companion lock file (<path>.lock) to serialize concurrent access.
    fn receives the current store and must return the updated store.
    """
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            store = _read_store(path)
            updated = fn(store)
            _atomic_write(path, updated)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


# =============================================================================
# Public API
# =============================================================================

def add_pending_agent(
    agent_id: str,
    description: str,
    chat_id: int,
    task_id: str | None = None,
    source: str = "telegram",
    output_file: str | None = None,
    timeout_minutes: int | None = None,
    path: Path = _DEFAULT_PATH,
) -> None:
    """Record a newly-spawned background agent.

    Args:
        agent_id:        Unique identifier for the agent (extracted from Task result).
        description:     Human-readable summary of what the agent is doing.
                         Include enough context so Lobster can relay results after restart.
        chat_id:         Chat/channel to notify when the agent completes.
        task_id:         Logical task identifier passed to write_result by the subagent.
        source:          Messaging platform ('telegram', 'slack', etc.).
        output_file:     Full path to the Claude Code agent output file in /tmp.
                         Enables liveness detection via mtime checks.
        timeout_minutes: Expected maximum runtime. Agents older than this can be
                         presumed dead if there is no recent output file activity.
        path:            Override the default pending-agents.json path (for testing).
    """
    entry = _make_agent_entry(
        agent_id=agent_id,
        description=description,
        chat_id=chat_id,
        task_id=task_id,
        source=source,
        output_file=output_file,
        timeout_minutes=timeout_minutes,
    )

    def update(store: dict) -> dict:
        agents = store.get("agents", [])
        # Avoid duplicate entries for the same ID
        filtered = _filter_out(agents, agent_id)
        return {"agents": filtered + [entry]}

    _with_lock(path, update)


def remove_pending_agent(
    agent_id: str,
    path: Path = _DEFAULT_PATH,
) -> None:
    """Remove a completed or failed agent from the pending list.

    Idempotent: removing a non-existent ID is a no-op.

    Args:
        agent_id: The ID to remove.
        path:     Override path (for testing).
    """
    def update(store: dict) -> dict:
        agents = store.get("agents", [])
        return {"agents": _filter_out(agents, agent_id)}

    _with_lock(path, update)


def get_pending_agents(path: Path = _DEFAULT_PATH) -> list:
    """Return a snapshot of all currently pending agents.

    Returns:
        List of agent dicts: [{"id", "description", "chat_id", "started_at"}, ...]
        Returns empty list if the file is missing or empty.
    """
    store = _read_store(path)
    return list(store.get("agents", []))


def is_agent_pending(agent_id: str, path: Path = _DEFAULT_PATH) -> bool:
    """Return True if the given agent_id is in the pending list.

    Args:
        agent_id: The ID to check.
        path:     Override path (for testing).
    """
    agents = get_pending_agents(path)
    return _find_agent(agents, agent_id) is not None


def pending_agent_count(path: Path = _DEFAULT_PATH) -> int:
    """Return the number of currently pending agents."""
    return len(get_pending_agents(path))
