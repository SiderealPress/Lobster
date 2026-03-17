#!/usr/bin/env python3
"""PostToolUse hook: auto-register spawned agents into agent_sessions.db.

Fires after every Agent tool call. Extracts structured metadata from the
agent prompt (YAML frontmatter or legacy "task_id is:" text), then inserts
a 'starting' row into agent_sessions.db so the spawned agent is visible to
the ghost detector and status queries without requiring the dispatcher to call
register_agent manually.

## Frontmatter format (preferred)

    ---
    task_id: my-task
    chat_id: 8305714125
    reply_to_message_id: 10924
    source: telegram
    ---

## Legacy text format (backward compat)

    Your task_id is: my-task

## Failure policy

On any error (malformed input, DB unavailable, etc.) this hook appends a
timestamped line to ~/lobster-workspace/logs/hook-failures.log and exits 0
so the Agent call is never blocked.

## settings.json configuration

Add this to ~/.claude/settings.json under "hooks" -> "PostToolUse":

    {
      "matcher": "Agent",
      "hooks": [
        {
          "type": "command",
          "command": "python3 $HOME/lobster/hooks/auto-register-agent.py",
          "timeout": 10
        }
      ]
    }
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_DB_PATH = _MESSAGES_DIR / "config" / "agent_sessions.db"
_LOG_PATH = (
    Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    / "logs"
    / "hook-failures.log"
)


# ---------------------------------------------------------------------------
# Logging (failures only -- never to stdout, never exit non-zero)
# ---------------------------------------------------------------------------

def _log_failure(message: str) -> None:
    """Append a timestamped failure entry to hook-failures.log."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with _LOG_PATH.open("a") as f:
            f.write(f"[{ts}] auto-register-agent: {message}\n")
    except Exception:  # noqa: BLE001
        pass  # If we can't log, there's nothing left to do


# ---------------------------------------------------------------------------
# Frontmatter / metadata extraction
# ---------------------------------------------------------------------------

def _parse_yaml_frontmatter(prompt: str) -> dict:
    """Extract key/value pairs from a YAML frontmatter block at the top of prompt.

    Only handles simple scalar key: value pairs (strings and integers). Does
    not import PyYAML to avoid external dependencies; the frontmatter format
    is intentionally constrained.

    Returns an empty dict if no valid frontmatter is found.
    """
    prompt = prompt.lstrip()
    if not prompt.startswith("---"):
        return {}

    # Find the closing ---
    rest = prompt[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}

    block = rest[:end].strip()
    result = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def _extract_task_id_from_text(prompt: str) -> str | None:
    """Fall back: extract task_id from legacy 'task_id is: X' pattern."""
    match = re.search(r"task_id\s+is:\s*(\S+)", prompt, re.IGNORECASE)
    return match.group(1) if match else None


def extract_metadata(prompt: str) -> dict:
    """Return a dict with task_id, chat_id, source, reply_to_message_id from prompt.

    Tries YAML frontmatter first. Falls back to text parsing for task_id.
    All values are strings (or None if absent).
    """
    fm = _parse_yaml_frontmatter(prompt)

    task_id = fm.get("task_id") or _extract_task_id_from_text(prompt)
    chat_id = fm.get("chat_id")
    source = fm.get("source", "telegram")
    reply_to_message_id = fm.get("reply_to_message_id")

    return {
        "task_id": task_id,
        "chat_id": str(chat_id) if chat_id is not None else None,
        "source": source or "telegram",
        "reply_to_message_id": (
            str(reply_to_message_id) if reply_to_message_id is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# Agent ID extraction from tool response
# ---------------------------------------------------------------------------

def extract_agent_id(tool_response: object) -> str | None:
    """Extract agentId from the Agent tool response.

    The response may be a dict or a list of content items. Handles both.
    """
    if isinstance(tool_response, dict):
        agent_id = tool_response.get("agentId")
        if agent_id:
            return str(agent_id)

    if isinstance(tool_response, list):
        for item in tool_response:
            if isinstance(item, dict):
                agent_id = item.get("agentId")
                if agent_id:
                    return str(agent_id)

    return None


def extract_output_file(tool_response: object) -> str | None:
    """Extract output_file from the Agent tool response if present."""
    if isinstance(tool_response, dict):
        output_file = tool_response.get("output_file") or tool_response.get("outputFile")
        if output_file:
            return str(output_file)

    if isinstance(tool_response, list):
        for item in tool_response:
            if isinstance(item, dict):
                output_file = item.get("output_file") or item.get("outputFile")
                if output_file:
                    return str(output_file)

    return None


# ---------------------------------------------------------------------------
# DB insert
# ---------------------------------------------------------------------------

def insert_agent_session(
    *,
    agent_id: str,
    task_id: str | None,
    chat_id: str | None,
    source: str,
    session_id: str,
    output_file: str | None,
) -> None:
    """Insert a 'starting' row into agent_sessions.db.

    Uses INSERT OR IGNORE so that a richer row written by register_agent
    (which may arrive concurrently) is left untouched.

    Raises on DB errors so the caller can log and swallow.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(_DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        # Ensure the table exists (minimal DDL -- session_store owns the full schema).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id                  TEXT PRIMARY KEY,
                task_id             TEXT,
                agent_type          TEXT,
                description         TEXT NOT NULL,
                chat_id             TEXT NOT NULL,
                source              TEXT NOT NULL DEFAULT 'telegram',
                status              TEXT NOT NULL DEFAULT 'running',
                output_file         TEXT,
                timeout_minutes     INTEGER,
                input_summary       TEXT,
                result_summary      TEXT,
                parent_id           TEXT,
                spawned_at          TEXT NOT NULL,
                completed_at        TEXT,
                last_seen_at        TEXT,
                notified_at         TEXT,
                trigger_message_id  TEXT,
                trigger_snippet     TEXT,
                reply_message_ids   TEXT
            )
        """)
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_sessions
                (id, task_id, agent_type, description, chat_id, source,
                 status, output_file, spawned_at)
            VALUES
                (?, ?, 'subagent', 'auto-registered by PostToolUse hook', ?, ?,
                 'starting', ?, ?)
            """,
            (
                agent_id,
                task_id,
                chat_id if chat_id is not None else "0",
                source,
                output_file,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        _log_failure(f"failed to parse hook input JSON: {exc}")
        sys.exit(0)

    # Only handle Agent tool calls
    tool_name = data.get("tool_name", "")
    if tool_name != "Agent":
        sys.exit(0)

    try:
        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response")
        session_id = data.get("session_id", "")

        prompt = tool_input.get("prompt", "")
        metadata = extract_metadata(prompt)
        agent_id = extract_agent_id(tool_response)
        output_file = extract_output_file(tool_response)

        if not agent_id:
            # No agent ID in response -- nothing to register
            sys.exit(0)

        insert_agent_session(
            agent_id=agent_id,
            task_id=metadata["task_id"],
            chat_id=metadata["chat_id"],
            source=metadata["source"],
            session_id=session_id,
            output_file=output_file,
        )

    except Exception as exc:  # noqa: BLE001
        _log_failure(f"unexpected error: {exc}")

    sys.exit(0)


if __name__ == "__main__":
    main()
