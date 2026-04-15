#!/usr/bin/env python3
"""
Stop hook: ensure subagents call write_result before exiting.
Injects a reminder if write_result was not called during the session.

Dispatcher sessions (detected via session_role.is_dispatcher()) are exempt —
the dispatcher never calls write_result, so the check only applies to subagents.

When write_result was called, this hook also marks the session completed in
agent_sessions.db synchronously — without relying on the unreliable server-side
auto-unregister path in inbox_server.py.

## Session ID strategy

The SubagentStop hook receives a session_id that is CC's internal UUID for the
subagent session. The agent_sessions.db can have two rows for the same subagent:

  1. A "proper" row: id = hex agentId (e.g. "a78e2e20dbc483b2e"), registered by
     the dispatcher via register_agent() after spawning the Task.

  2. An "auto-register stub": id = session UUID (e.g. "29e27af2-..."), created by
     the SessionStart hook (write-dispatcher-session-id.py) when the subagent's
     session starts.

To close both rows we call session_end() with up to three identifiers:
  a. The task_id from the write_result call input (matches the `task_id` column
     when the dispatcher set task_id in register_agent, or when the subagent uses
     a stable task_id that both parties know).
  b. The session_id from hook data (matches the auto-register stub by `id`).

The proper hex-ID row is the primary target but is only reachable when
task_id matches. When it is not reachable via this hook, the reconciler in
inbox_server.py will close it when it detects stop_reason=end_turn in the
output file.

## SubagentStop vs Stop transcript handling

Neither SubagentStop nor Stop hooks receive an inline `transcript` field in
CC 2.1.76+. Both pass a file path instead:
- Stop: `transcript_path` (JSONL file of the current session's conversation)
- SubagentStop: `agent_transcript_path` (JSONL file of the subagent's conversation)

This hook reads the appropriate file path for each event type. An inline
`transcript` field is supported as a legacy fallback for older CC versions.

## JSONL message format

Each line of the JSONL transcript file has the structure:
    {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

Tool use items are nested under entry["message"]["content"], NOT entry["content"].
`_collect_tool_use_and_text` handles both the JSONL format and the legacy inline
format where content is directly on the message dict.

## Suppressing feedback injection on success

Claude Code injects a "Stop hook feedback: ... No stderr output" system message
into the agent even when the hook exits 0. To prevent this feedback from
triggering a new agent turn, the hook outputs JSON with `{"suppressOutput": true}`
on all success paths. Per the CC hook spec, this suppresses feedback injection.

## Fallback after N retry fires

When write_result is not called and the hook blocks with exit 2, CC re-runs the
subagent with the error message injected. If the subagent still cannot call
write_result (e.g. turn exhaustion, crash loop), the hook fires repeatedly and
the agent never terminates.

After MAX_HOOK_FIRES fires without a successful write_result, the hook gives up
blocking and emits a synthetic subagent_result to ~/messages/inbox/ with the
last meaningful transcript content (extracted from turns BEFORE the hook started
firing). This ensures the dispatcher always gets something instead of nothing.

Fire count is tracked in /tmp/lobster-hook-fires-{agent_key} as JSON:
    {"count": N, "first_fire_ts": <unix timestamp>}

The file is cleaned up after the fallback emit.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher, get_session_id

# JSON to emit on every successful (allow) exit — suppresses the
# "Stop hook feedback: No stderr output" injection that CC 2.1.76+ produces
# even when the hook exits 0 with no output.
_SILENT_OK = json.dumps({"suppressOutput": True})

# Maximum number of hook fires before giving up and emitting a synthetic result.
MAX_HOOK_FIRES = 5

# Number of pre-hook transcript turns to extract for the synthetic result.
_FALLBACK_TURNS = 3


def _exit_ok() -> None:
    """Exit 0 with JSON that suppresses CC feedback injection."""
    print(_SILENT_OK)
    sys.exit(0)


def _extract_write_result_task_ids(all_tool_use_items: list) -> list[str]:
    """Return all non-empty task_id values from write_result tool call inputs.

    The task_id passed to write_result often matches either the `id` column
    (when the subagent uses the hex agentId as task_id) or the `task_id` column
    (when the dispatcher set task_id in register_agent). session_end() matches
    on id OR task_id, so trying every task_id found in write_result calls gives
    the best chance of closing the correct DB row.

    Returns a list of unique non-empty task_id strings (order preserved).
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in all_tool_use_items:
        if item.get("name") == "mcp__lobster-inbox__write_result":
            tid = item.get("input", {}).get("task_id")
            if tid and isinstance(tid, str) and tid not in seen:
                seen.add(tid)
                result.append(tid)
    return result


def _mark_session_completed(id_or_task_id: str) -> None:
    """Mark the agent session completed in agent_sessions.db.

    Best-effort: any failure is silently swallowed so the hook always exits 0.
    Uses session_store.session_end() which matches on id OR task_id and is
    idempotent — safe to call even if inbox_server already updated the row.
    """
    try:
        hooks_dir = Path(__file__).parent
        repo_src = hooks_dir.parent / "src"
        sys.path.insert(0, str(repo_src))
        from agents.session_store import session_end  # noqa: PLC0415
        session_end(
            id_or_task_id=id_or_task_id,
            status="completed",
            result_summary="Completed via SubagentStop hook",
        )
    except Exception:
        pass  # DB update is best-effort; never block exit


def _mark_session_notified(id_or_task_id: str) -> None:
    """Set notified_at on the agent session row in agent_sessions.db.

    Best-effort: any failure is silently swallowed so the hook always exits 0.
    Uses session_store.set_notified() which matches on id OR task_id and is
    idempotent — safe to call even if handle_write_result already set it.

    This prevents the reconciler from treating the row as unnotified and
    enqueuing a duplicate subagent_notification message. Must be called
    AFTER _mark_session_completed so the row is in a terminal state first.
    """
    try:
        hooks_dir = Path(__file__).parent
        repo_src = hooks_dir.parent / "src"
        sys.path.insert(0, str(repo_src))
        from agents.session_store import set_notified  # noqa: PLC0415
        set_notified(id_or_task_id)
    except Exception:
        pass  # DB update is best-effort; never block exit


def _load_transcript_from_jsonl(path: str) -> list:
    """Load transcript messages from a JSONL file.

    SubagentStop passes agent_transcript_path (a .jsonl file) rather than an
    inline transcript list. Each line is a JSON object. Returns [] on any error.
    """
    try:
        messages = []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return messages
    except Exception:
        return []


def _collect_tool_use_and_text(transcript: list) -> tuple[list, list]:
    """Walk a transcript and return (tool_use_items, text_content_parts).

    Handles both JSONL format (CC 2.1.76+) and legacy inline format:

    JSONL format (each line is a JSONL entry):
        {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

    Legacy inline format (transcript is a list of messages):
        {"role": "assistant", "content": [...]}

    Both formats are tried so the hook works regardless of CC version.
    """
    all_tool_use_items = []
    text_content_parts = []
    for entry in transcript:
        if not isinstance(entry, dict):
            continue

        # JSONL format: content is under entry["message"]["content"]
        # Legacy format: content is directly under entry["content"]
        nested_msg = entry.get("message")
        if isinstance(nested_msg, dict):
            content = nested_msg.get("content", [])
        else:
            content = entry.get("content", [])

        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                all_tool_use_items.append(item)
            elif item.get("type") == "text":
                text_content_parts.append(item.get("text", ""))
    return all_tool_use_items, text_content_parts


# ---------------------------------------------------------------------------
# Retry-fire tracking
# ---------------------------------------------------------------------------

def _agent_key(data: dict) -> str:
    """Return a stable key for the fire-count temp file.

    Prefer agent_id (SubagentStop) over session_id (Stop). Falls back to
    a constant so the temp file path is always well-defined.
    """
    agent_id = data.get("agent_id") or ""
    session_id = data.get("session_id") or ""
    key = agent_id or session_id or "unknown"
    # Sanitise: keep only alphanumeric, dash, and dot characters.
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in key)


def _fire_count_path(agent_key: str) -> Path:
    """Return the Path of the temp file that tracks hook fires for this agent."""
    return Path(f"/tmp/lobster-hook-fires-{agent_key}")


def _read_fire_state(path: Path) -> tuple[int, float]:
    """Read (fire_count, first_fire_ts) from the temp file.

    Returns (0, 0.0) if the file is absent or unreadable.
    """
    try:
        state = json.loads(path.read_text())
        count = int(state.get("count", 0))
        first_ts = float(state.get("first_fire_ts", 0.0))
        return count, first_ts
    except Exception:
        return 0, 0.0


def _write_fire_state(path: Path, count: int, first_fire_ts: float) -> None:
    """Write (count, first_fire_ts) to the temp file. Best-effort."""
    try:
        path.write_text(json.dumps({"count": count, "first_fire_ts": first_fire_ts}))
    except Exception:
        pass


def _cleanup_fire_state(path: Path) -> None:
    """Remove the fire-count temp file. Best-effort."""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _increment_fire_count(data: dict) -> tuple[int, float]:
    """Increment the fire count for this agent and return (new_count, first_fire_ts).

    On the first fire, records the current timestamp as first_fire_ts.
    """
    key = _agent_key(data)
    path = _fire_count_path(key)
    count, first_fire_ts = _read_fire_state(path)

    now = time.time()
    count += 1
    if count == 1:
        first_fire_ts = now

    _write_fire_state(path, count, first_fire_ts)
    return count, first_fire_ts


# ---------------------------------------------------------------------------
# Fallback: extract pre-hook transcript content and write synthetic result
# ---------------------------------------------------------------------------

def _entry_timestamp(entry: dict) -> float:
    """Extract a unix timestamp from a JSONL transcript entry.

    CC 2.1.76+ transcript entries have a 'timestamp' field (ISO-8601 or epoch).
    Returns 0.0 if absent or unparseable — entries without timestamps are
    treated as pre-hook (included in fallback content).
    """
    ts = entry.get("timestamp")
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        # Try ISO-8601 first, then numeric string.
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        try:
            return float(ts)
        except ValueError:
            return 0.0
    return 0.0


def _extract_pre_hook_text(transcript: list, first_fire_ts: float, n_turns: int) -> str:
    """Extract meaningful text from transcript turns that predate the hook firing.

    Filters to entries whose timestamp < first_fire_ts (or all entries if
    first_fire_ts is 0 / timestamps are missing), then takes the last n_turns
    assistant text blocks and joins them.

    Returns a non-empty string, or an empty string if nothing useful was found.
    """
    # Select entries that predate the first hook fire.
    # If first_fire_ts is 0 (unknown), include all entries.
    if first_fire_ts > 0:
        pre_hook = [e for e in transcript if _entry_timestamp(e) < first_fire_ts]
    else:
        pre_hook = list(transcript)

    # Collect text parts from the pre-hook portion of the transcript.
    # Walk entries in order so we can take the last N meaningful turns.
    turns = []
    for entry in pre_hook:
        if not isinstance(entry, dict):
            continue
        nested_msg = entry.get("message")
        if isinstance(nested_msg, dict):
            content = nested_msg.get("content", [])
        else:
            content = entry.get("content", [])
        if not isinstance(content, list):
            continue

        texts = [
            item.get("text", "").strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        combined = "\n".join(t for t in texts if t)
        if combined:
            turns.append(combined)

    # Take the last n_turns non-empty turns.
    recent_turns = turns[-n_turns:] if turns else []
    return "\n\n---\n\n".join(recent_turns)


def _write_synthetic_inbox_message(
    data: dict,
    content: str,
    task_id_hint: str,
) -> None:
    """Write a synthetic subagent_result message to ~/messages/inbox/.

    The message has the same JSON structure as a normal write_result inbox
    message (type='subagent_result', status='success'), with a note that the
    content was recovered from the transcript after the agent failed to call
    write_result. chat_id defaults to 0 (system route) when not discoverable.

    Best-effort: any failure is silently swallowed.
    """
    try:
        inbox_dir = Path(os.path.expanduser("~/messages/inbox"))
        inbox_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)

        # Derive a task_id: prefer the hint extracted from the transcript,
        # fall back to session/agent id, then a generic fallback.
        task_id = task_id_hint or data.get("agent_id") or data.get("session_id") or "recovered-agent"
        safe_task_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)[:40]
        message_id = f"{ts_ms}_{safe_task_id}_recovered"

        # chat_id: we don't know the original chat_id when the agent didn't call
        # write_result. Use 0 as the dispatcher system route so the dispatcher
        # can decide what to do with the recovered result.
        chat_id = 0

        recovery_note = (
            "Agent exited without calling write_result. "
            f"Content recovered from transcript after {MAX_HOOK_FIRES} hook fires."
        )

        if content:
            text = f"{recovery_note}\n\nRecovered content:\n\n{content}"
        else:
            text = f"{recovery_note}\n\n(No recoverable transcript content found.)"

        message = {
            "id": message_id,
            "type": "subagent_recovered",
            "source": "system",
            "chat_id": chat_id,
            "text": text,
            "task_id": task_id,
            "status": "recovered",
            "sent_reply_to_user": False,  # dispatcher must relay this; user has NOT been notified
            "timestamp": now.isoformat(),
            "recovered": True,
        }

        inbox_file = inbox_dir / f"{message_id}.json"
        # Atomic write: write to .tmp then rename.
        tmp_file = inbox_file.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(message, indent=2))
        tmp_file.rename(inbox_file)
    except Exception:
        pass  # Never block exit on fallback emit failure



def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        _exit_ok()  # If we can't read input, don't block

    # Dispatcher sessions are exempt — skip the write_result check.
    # session_role.is_dispatcher() uses the marker file as primary signal and
    # the transcript (which is present in Stop hooks) as fallback.
    if is_dispatcher(data):
        _exit_ok()

    hook_event = data.get("hook_event_name", "")
    is_subagentstop = hook_event == "SubagentStop"

    if is_subagentstop:
        # SubagentStop: transcript is in a JSONL file at agent_transcript_path.
        # CC does NOT include an inline transcript field for this event.
        transcript_path = data.get("agent_transcript_path", "")
        if not transcript_path:
            # No path provided — can't verify; allow exit to avoid blocking.
            _exit_ok()
        transcript = _load_transcript_from_jsonl(transcript_path)
    else:
        # Stop hook: CC 2.1.76+ passes transcript_path (JSONL file), not inline.
        # Try the file path first; fall back to inline transcript[] for older CC
        # versions that may still embed the transcript directly.
        transcript_path = data.get("transcript_path", "")
        if transcript_path:
            transcript = _load_transcript_from_jsonl(transcript_path)
        else:
            # Older CC: transcript may be inline (legacy fallback).
            transcript = data.get("transcript", [])

    # Collect all tool call items so we can inspect both name and input.
    # Also collect text content parts for pseudocode detection.
    all_tool_use_items, text_content_parts = _collect_tool_use_and_text(transcript)

    tool_call_names = [item.get("name", "") for item in all_tool_use_items]

    if "mcp__lobster-inbox__write_result" in tool_call_names:
        # write_result was called — verify it was called with a non-null chat_id.
        # chat_id=0 is explicitly allowed: it is the dispatcher system route for
        # background agents that were spawned without a user chat_id.
        write_result_items = [
            item for item in all_tool_use_items
            if item.get("name") == "mcp__lobster-inbox__write_result"
        ]
        valid_calls = [
            item for item in write_result_items
            if item.get("input", {}).get("chat_id") is not None
        ]
        if valid_calls:
            # At least one valid call found — mark session(s) and allow exit.
            #
            # Use multiple identifiers to maximise the chance of closing the
            # correct DB row (see module docstring for why multiple rows exist):
            #
            #   1. task_id(s) from write_result inputs — reaches the "proper"
            #      hex-agentId row when task_id matches (id OR task_id column).
            #   2. session_id from hook data — reaches the auto-register stub
            #      row (id = session UUID).
            #
            # All calls are idempotent, so calling the same id twice is safe.
            write_result_task_ids = _extract_write_result_task_ids(all_tool_use_items)
            session_id = get_session_id(data)

            for tid in write_result_task_ids:
                _mark_session_completed(tid)
            if session_id:
                _mark_session_completed(session_id)

            # Set notified_at on all rows so the reconciler never sees them as
            # unnotified and enqueues duplicate subagent_notification messages.
            # Order: complete first, notify second (terminal state before marking
            # notified is the correct sequence).
            #
            # Belt-and-suspenders for the task_id rows: handle_write_result in
            # inbox_server.py also calls set_notified, but race conditions or
            # server restarts can leave notified_at NULL. Setting it here too
            # closes that gap synchronously in the hook.
            #
            # The session_id stub row is the primary target: it is never touched
            # by handle_write_result (which matches on task_id, not session UUID),
            # so without this call the stub row would permanently have
            # notified_at IS NULL and trigger duplicate notifications on reconcile.
            for tid in write_result_task_ids:
                _mark_session_notified(tid)
            if session_id:
                _mark_session_notified(session_id)

            # Clean up any fire-count temp file — the agent eventually called
            # write_result, so reset the counter for this session.
            key = _agent_key(data)
            _cleanup_fire_state(_fire_count_path(key))

            _exit_ok()
        else:
            # write_result was called but chat_id was None in every call —
            # the MCP server rejected the call and the result was not stored.
            print(
                "STOP: write_result was called but chat_id was None in every call. "
                "The MCP server rejects write_result without a chat_id, so your result "
                "was not delivered. "
                "Call write_result again with a valid chat_id. "
                "If you were not given a user chat_id, use chat_id=0 — that is the "
                "dispatcher system route for background agents with no user context.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Subagent finished without calling write_result.
    # Increment the fire counter and decide whether to block or fall back.
    fire_count, first_fire_ts = _increment_fire_count(data)

    if fire_count > MAX_HOOK_FIRES:
        # Give up blocking — extract the best pre-hook content and emit a
        # synthetic subagent_result so the dispatcher gets something.
        task_id_hint = ""
        # Try to extract a task_id hint from the prompt text in the transcript.
        # A common pattern is "Your task_id is: <id>" injected by the dispatcher.
        for part in text_content_parts:
            if "task_id" in part.lower():
                import re
                m = re.search(r"task[_\s-]?id\s*(?:is\s*)?[:\-]?\s*([A-Za-z0-9_-]+)", part, re.IGNORECASE)
                if m:
                    task_id_hint = m.group(1)
                    break

        content = _extract_pre_hook_text(transcript, first_fire_ts, _FALLBACK_TURNS)
        _write_synthetic_inbox_message(data, content, task_id_hint)

        # Clean up the fire-count temp file.
        key = _agent_key(data)
        _cleanup_fire_state(_fire_count_path(key))

        _exit_ok()

    # Still within the retry window — block with exit 2 and a reminder message.
    fires_remaining = MAX_HOOK_FIRES - fire_count
    # Check whether write_result appeared as text output (pseudocode failure mode)
    # to give a more actionable error message.
    combined_text = "\n".join(text_content_parts)
    if "mcp__lobster-inbox__write_result" in combined_text:
        print(
            "STOP: write_result was described as text but not called as a tool.\n\n"
            "The tool call appeared in your text output as Python code — this is a "
            "description, not an invocation. You must call write_result using the tool "
            "invocation mechanism (the same way you call Read, Edit, Bash, etc.) — not "
            "by writing it as code output.\n\n"
            f"Call write_result now using the tool mechanism. "
            f"({fires_remaining} attempt(s) remaining before the hook gives up.)",
            file=sys.stderr,
        )
    else:
        print(
            "STOP: You must call mcp__lobster-inbox__write_result before finishing. "
            "The dispatcher is waiting for your result. "
            "If the task failed, report the failure — but you must call write_result. "
            f"Call it now with your findings, then you may exit. "
            f"({fires_remaining} attempt(s) remaining before the hook gives up.)",
            file=sys.stderr,
        )
    sys.exit(2)  # Exit 2 to hard-block the session from terminating


if __name__ == "__main__":
    main()
