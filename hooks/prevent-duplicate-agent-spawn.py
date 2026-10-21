#!/usr/bin/env python3
"""PreToolUse hook: blocks an Agent/Task spawn if another agent is already
``status: 'running'`` for the same logical unit of work.

## Why this exists (issue #2313)

Three incidents on 2026-10-20/21 showed that nothing in Lobster's hook chain
sits *before* an Agent/Task spawn to ask "is this duplicate work already in
flight?" ``auto-register-agent.py`` (PostToolUse) records bookkeeping only
*after* a spawn has already happened -- by the time it runs, the duplicate
subagent is already live and may already be making external side effects
(the worst incident: two concurrent runs of the x-bookmarks scheduled job
posted ~46-51 duplicate `gh issue comment` calls and sent two overlapping
Telegram digests, racing on the same unlocked state file).

This hook closes that gap by querying ``agent_sessions.db`` for an existing
``running``/``starting`` row that collides with the new spawn **before**
``Agent``/``Task`` is allowed to execute at all.

## Collision rule

A new spawn collides with an existing running session when, within a recent
window (default 30 minutes -- generous on purpose: false negatives here are
far cheaper than false positives blocking legitimate sequential work), either:

1. **Same non-empty task_id.** Exact match. Covers the case where the same
   logical task is re-spawned under an identical task_id (e.g. a scheduled
   job's task_id, or a dispatcher retry that reuses the same slug).
2. **Same non-empty trigger_message_id.** Exact match. Covers the case where
   two spawns are both caused by the *same inbound message* (confirmed
   mechanism in shadowlobster#94's incident 3: one cron-side dispatch,
   multiple independent Agent spawns downstream of it) -- this catches
   duplication even when the two spawns end up with *different* task_ids
   (which is exactly what happened in the Mark Henry incident: three
   different task_ids for the same logical edit).

Nested spawns (where the new spawn's prompt frontmatter carries a task_id
that is a child of an existing running parent, by design, e.g. a
``functional-engineer`` legitimately re-entering its own flow) are NOT
specially exempted here -- the agent-definition tool restriction (see
``.claude/agents/*.md`` ``tools:`` frontmatter) is what prevents designed-in
nested spawns from happening in the first place for leaf-work agents. This
hook is a backstop against *any* collision, nested or not: if a nested spawn
somehow still occurs (e.g. a future agent type that legitimately has
Agent/Task access), colliding on the same task_id/trigger_message_id as its
own parent is still a duplicate worth blocking.

## Fail-open boundary (important, read before changing)

This hook fails OPEN (exit 0, allow the spawn) on any *infrastructure*
problem: malformed stdin JSON, DB file missing/unreadable, unexpected
schema. It does NOT fail open once a collision is genuinely found in a
successful query -- blocking the collision is the entire point, and silently
allowing it "to be safe" would reintroduce the exact bug this hook exists to
close. The only failure mode that favours availability over strictness is
"we could not determine whether a collision exists," not "we found one and
decided to let it through anyway."

## Helper agents and missing identifiers

Built-in helper agents (``Explore``, ``Plan`` by default, see
``hooks/helper_agent_types.py``) are exempt: they never carry a task_id and
are not part of Lobster's subagent protocol.

A spawn with neither a non-empty task_id nor a non-empty trigger_message_id
has nothing to collide on and is allowed through unconditionally -- this
hook only ever blocks on a *positive* match against real identifiers, never
on the absence of them.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helper_agent_types import is_helper_agent_type  # noqa: E402
from spawn_frontmatter import parse_frontmatter  # noqa: E402

# Tool names used to spawn subagents across CC versions.
AGENT_TOOL_NAMES = {"Agent", "Task"}

_MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", Path.home() / "messages"))
_DB_PATH = _MESSAGES_DIR / "config" / "agent_sessions.db"
_LOG_PATH = (
    Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    / "logs"
    / "hook-failures.log"
)

# How far back a running session is still considered a live collision
# candidate. Generous on purpose -- see module docstring.
DEFAULT_COLLISION_WINDOW_MINUTES = 30
_WINDOW_ENV = "LOBSTER_DEDUP_COLLISION_WINDOW_MINUTES"


def _log(message: str) -> None:
    """Best-effort append to hook-failures.log. Never raises."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with _LOG_PATH.open("a") as f:
            f.write(f"[{ts}] prevent-duplicate-agent-spawn: {message}\n")
    except Exception:  # noqa: BLE001
        pass


def _collision_window_minutes() -> float:
    raw = os.environ.get(_WINDOW_ENV, "").strip()
    if not raw:
        return DEFAULT_COLLISION_WINDOW_MINUTES
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_COLLISION_WINDOW_MINUTES
    except ValueError:
        return DEFAULT_COLLISION_WINDOW_MINUTES


def _parse_spawned_at(value: str) -> datetime | None:
    """Parse the spawned_at column, which may be ISO-with-offset or the
    'YYYY-MM-DD HH:MM:SS' (UTC, naive) format auto-register-agent.py writes.
    Returns None (not raises) on anything unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def find_collision(
    conn: sqlite3.Connection,
    *,
    task_id: str | None,
    trigger_message_id: str | None,
    new_agent_hint: str | None,
    now: datetime,
    window_minutes: float,
) -> dict | None:
    """Return the first colliding running/starting row, or None.

    Pure-ish: takes an open connection and `now` as inputs so it is testable
    without real wall-clock time or a live DB fixture beyond an in-memory one.
    `new_agent_hint` (the caller's own agent_id, i.e. parent_id of the new
    spawn) is excluded from the match so a subagent's own still-running row
    is never flagged as colliding with the spawn it is itself making.
    """
    cutoff = now - timedelta(minutes=window_minutes)
    cutoff_str = cutoff.isoformat()

    clauses = []
    params: list[str] = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if trigger_message_id:
        clauses.append("trigger_message_id = ?")
        params.append(trigger_message_id)

    if not clauses:
        return None

    query = f"""
        SELECT id, task_id, trigger_message_id, description, spawned_at, status
        FROM agent_sessions
        WHERE status IN ('running', 'starting')
          AND ({' OR '.join(clauses)})
        ORDER BY spawned_at ASC
    """
    rows = conn.execute(query, params).fetchall()
    for row in rows:
        row_id, row_task_id, row_trigger_id, description, spawned_at, status = row
        if new_agent_hint and row_id == new_agent_hint:
            continue
        spawned_dt = _parse_spawned_at(spawned_at)
        if spawned_dt is not None and spawned_dt < cutoff:
            continue  # outside the window -- treat as stale, not a live collision
        return {
            "id": row_id,
            "task_id": row_task_id,
            "trigger_message_id": row_trigger_id,
            "description": description,
            "spawned_at": spawned_at,
            "status": status,
        }
    return None


def _format_block_message(collision: dict, *, task_id: str | None, trigger_message_id: str | None) -> str:
    matched_on = "task_id" if collision.get("task_id") == task_id and task_id else "trigger_message_id"
    return (
        "BLOCKED: an agent is already running for this request. "
        f"Existing session id={collision['id']!r}, task_id={collision['task_id']!r}, "
        f"spawned_at={collision['spawned_at']!r} (matched on {matched_on}). "
        "Do not spawn a duplicate -- if the existing agent is genuinely stuck or "
        "dead, use TaskStop or wait for the ghost detector rather than spawning a "
        "second one for the same work. If this is intentionally a distinct new "
        "task that happens to reuse an identifier, use a different task_id."
    )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        _log(f"failed to parse hook input JSON (fail-open): {exc}")
        sys.exit(0)

    tool = data.get("tool_name", "")
    if tool not in AGENT_TOOL_NAMES:
        sys.exit(0)

    try:
        inp = data.get("tool_input", {})
        subagent_type = inp.get("subagent_type")
        if is_helper_agent_type(subagent_type):
            sys.exit(0)

        prompt = inp.get("prompt", "")
        frontmatter = parse_frontmatter(prompt)
        task_id = (frontmatter.get("task_id") or "").strip() or None
        trigger_message_id = (frontmatter.get("trigger_message_id") or "").strip() or None

        if not task_id and not trigger_message_id:
            # Nothing to collide on. Legacy-text-format task_id (the
            # "task_id is: X" fallback) is intentionally not parsed here --
            # require-task-id-in-prompt.py still enforces a frontmatter or
            # legacy task_id exists; this hook only acts when it has a
            # structured identifier confident enough to match on.
            sys.exit(0)

        # The caller's own agent_id, when this spawn happens from inside an
        # already-registered subagent (see auto-register-agent.py for the
        # same signal) -- excluded from collision matching so a subagent
        # never collides with its own still-running row.
        caller_agent_id = data.get("agent_id") or None

        if not _DB_PATH.exists():
            # No DB yet means no prior sessions exist to collide with.
            sys.exit(0)

        conn = sqlite3.connect(str(_DB_PATH))
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            collision = find_collision(
                conn,
                task_id=task_id,
                trigger_message_id=trigger_message_id,
                new_agent_hint=caller_agent_id,
                now=datetime.now(timezone.utc),
                window_minutes=_collision_window_minutes(),
            )
        finally:
            conn.close()

    except Exception as exc:  # noqa: BLE001
        # Infrastructure failure (DB unreadable, unexpected schema, etc.) --
        # fail open. See module docstring: this is the ONLY fail-open path.
        _log(f"unexpected error during collision check (fail-open): {exc}")
        sys.exit(0)

    if collision is None:
        sys.exit(0)

    _log(
        f"BLOCKED duplicate spawn: new task_id={task_id!r} "
        f"trigger_message_id={trigger_message_id!r} collided with "
        f"existing id={collision['id']!r} task_id={collision['task_id']!r}"
    )
    print(
        _format_block_message(collision, task_id=task_id, trigger_message_id=trigger_message_id),
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
