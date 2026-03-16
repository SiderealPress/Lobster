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
"""
import json
import sys
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher, get_session_id

# JSON to emit on every successful (allow) exit — suppresses the
# "Stop hook feedback: No stderr output" injection that CC 2.1.76+ produces
# even when the hook exits 0 with no output.
_SILENT_OK = json.dumps({"suppressOutput": True})


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

    # Subagent finished without calling write_result — block exit.
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
            "Call write_result now using the tool mechanism.",
            file=sys.stderr,
        )
    else:
        print(
            "STOP: You must call mcp__lobster-inbox__write_result before finishing. "
            "The dispatcher is waiting for your result. "
            "If the task failed, report the failure — but you must call write_result. "
            "Call it now with your findings, then you may exit.",
            file=sys.stderr,
        )
    sys.exit(2)  # Exit 2 to hard-block the session from terminating


if __name__ == "__main__":
    main()
