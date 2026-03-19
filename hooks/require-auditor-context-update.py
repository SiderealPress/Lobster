#!/usr/bin/env python3
"""
SubagentStop hook: ensure lobster-auditor sessions update system-audit.context.md.

For non-auditor sessions this hook is a no-op (exits 0 immediately).

For auditor sessions it enforces one of two exit conditions:
  1. system-audit.context.md was modified during this session
     (mtime >= session start time found in the transcript), OR
  2. the transcript contains the safe word AUDIT_CONTEXT_UNCHANGED
     (agent explicitly confirmed that nothing new was found).

If neither condition is met the hook prints an error message and exits 2,
hard-blocking the session from terminating until the agent complies.

Detection strategy for "is this an auditor session":
  Scan the transcript for a ReadFile tool call whose path contains
  "system-audit.context.md". The auditor definition requires reading this
  file at session start, so its presence is a reliable signal.

## SubagentStop transcript handling (CC 2.1.76+)

SubagentStop events in CC 2.1.76+ no longer include an inline `transcript`
field. They only provide `agent_transcript_path` — a path to a JSONL file
containing the subagent's conversation. This hook loads the transcript from
that file path, falling back to the legacy inline `transcript` key for older
CC versions.

## JSONL message format

Each line of the JSONL transcript file has the structure:
    {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

Tool use items are nested under entry["message"]["content"], NOT entry["content"].
`_extract_tool_calls` handles both the JSONL format and the legacy inline
format where content is directly on the message dict.

## Circuit breaker (MAX_HOOK_FIRES)

If the auditor agent cannot satisfy the exit conditions and the hook keeps
blocking (e.g. turn exhaustion, crash loop), the hook would fire indefinitely.
To prevent runaway sessions, after MAX_HOOK_FIRES fires without the condition
being met the hook logs a loud system_error entry and allows the exit (exit 0).

MAX_HOOK_FIRES = 3 (lower than require-write-result's 5) because repeated
firing here means the auditor ran, read the context file, but never satisfied
the post-condition — a serious signal that something is structurally wrong with
the auditor agent or its environment.

Fire count is tracked in /tmp/lobster-auditor-hook-fires-{agent_key} as JSON:
    {"count": N, "first_fire_ts": <unix timestamp>}

The file is cleaned up after a successful exit (either condition met) or after
the circuit breaker trips.

## suppressOutput

This hook exits silently (no stdout/stderr) on all exit-0 paths.
require-write-result.py (which runs first for the same SubagentStop event)
already emits {"suppressOutput": true} and covers the event. Emitting a
second suppressOutput here causes CC to surface it as feedback content
rather than suppress it.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def _exit_ok() -> None:
    """Exit 0 silently.

    Intentionally produces no stdout or stderr output. For SubagentStop hooks,
    suppressOutput in stdout is not reliably honoured by CC when multiple hooks
    are registered for the same event — the first hook (require-write-result.py)
    already emits suppressOutput and covers the event. Adding a second
    suppressOutput from this hook causes CC to report it as feedback rather than
    suppress it. Silence is the correct behaviour here.
    """
    sys.exit(0)

CONTEXT_FILE = Path(os.path.expanduser(
    "~/lobster-user-config/agents/system-audit.context.md"
))

SAFE_WORD = "AUDIT_CONTEXT_UNCHANGED"

# Path fragment used to detect auditor sessions in the transcript.
AUDIT_CONTEXT_FILENAME = "system-audit.context.md"

# Maximum hook fires before the circuit breaker trips and allows exit.
# Lower than require-write-result's 5 because exhaustion here is a serious signal.
MAX_HOOK_FIRES = 3

# Log file for system-level errors (circuit breaker trips, etc.)
_OBSERVATIONS_LOG = Path(os.path.expanduser("~/lobster-workspace/logs/observations.log"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _extract_tool_calls(transcript: list) -> list[dict]:
    """Return all tool_use blocks from the transcript.

    Handles both JSONL format (CC 2.1.76+) and legacy inline format:

    JSONL format (each line is a JSONL entry):
        {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

    Legacy inline format (transcript is a list of messages):
        {"role": "assistant", "content": [...]}

    Both formats are tried so the hook works regardless of CC version.
    """
    tool_calls = []
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
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_calls.append(item)
    return tool_calls


def _is_auditor_session(tool_calls: list[dict]) -> bool:
    """Return True if any tool call reads system-audit.context.md.

    The auditor subagent definition requires reading this file at the start of
    every session.  A Read/cat/Bash call whose input references the filename is
    sufficient evidence.
    """
    for call in tool_calls:
        name = call.get("name", "")
        # Check Read tool (Claude Code built-in)
        if name == "Read":
            path = call.get("input", {}).get("file_path", "")
            if AUDIT_CONTEXT_FILENAME in path:
                return True
        # Check Bash tool — auditor might cat/head the file
        if name == "Bash":
            cmd = call.get("input", {}).get("command", "")
            if AUDIT_CONTEXT_FILENAME in cmd:
                return True
    return False


def _safe_word_in_transcript(tool_calls: list[dict]) -> bool:
    """Return True if write_result was called with AUDIT_CONTEXT_UNCHANGED."""
    for call in tool_calls:
        if call.get("name") != "mcp__lobster-inbox__write_result":
            continue
        inp = call.get("input", {})
        text = inp.get("text", "")
        if SAFE_WORD in text:
            return True
    return False


def _parse_timestamp(t: str | int | float | None) -> float | None:
    """Parse a timestamp value to a Unix float.

    Accepts:
      - int/float: returned as float directly
      - str: tried as ISO-8601 first (CC 2.1.76+ format), then as a numeric string

    Returns None if the value cannot be parsed or if t is None.
    """
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        # ISO-8601 (CC 2.1.76+): e.g. "2026-03-19T16:52:01.657Z"
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        # Numeric string fallback (older CC versions)
        try:
            return float(t)
        except ValueError:
            pass
    return None


def _session_start_time(hook_input: dict, transcript: list) -> float | None:
    """Estimate session start time from the transcript's first message timestamp.

    Falls back to None if no timestamp is available (hook input varies).

    Accepts the already-loaded transcript list (which may have been read from
    a JSONL file) rather than re-reading hook_input["transcript"], which is
    always empty in CC 2.1.76+.
    """
    # Claude Code hook input may carry a top-level timestamp.
    ts = hook_input.get("session_start_time") or hook_input.get("timestamp")
    if ts:
        parsed = _parse_timestamp(ts)
        if parsed is not None:
            return parsed

    # Try to find the minimum timestamp inside transcript messages.
    min_ts = None
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        t = msg.get("timestamp")
        if t:
            t_float = _parse_timestamp(t)
            if t_float is not None:
                if min_ts is None or t_float < min_ts:
                    min_ts = t_float
    return min_ts


def _context_file_updated_since(since: float | None) -> bool:
    """Return True if system-audit.context.md was modified at or after `since`.

    If `since` is None (unknown session start), we cannot verify via mtime —
    return False so the safe word remains the only exit path.
    """
    if since is None:
        return False
    try:
        mtime = CONTEXT_FILE.stat().st_mtime
        # Allow a 1-second clock skew margin.
        return mtime >= (since - 1.0)
    except OSError:
        # File doesn't exist yet — not updated.
        return False


# ---------------------------------------------------------------------------
# Circuit breaker: fire-count tracking
# ---------------------------------------------------------------------------


def _agent_key(hook_input: dict) -> str:
    """Return a stable key for the fire-count temp file.

    Prefer agent_id (SubagentStop) over session_id (Stop). Falls back to
    a constant so the temp file path is always well-defined.
    """
    agent_id = hook_input.get("agent_id") or ""
    session_id = hook_input.get("session_id") or ""
    key = agent_id or session_id or "unknown"
    # Sanitise: keep only alphanumeric, dash, dot, and underscore characters.
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in key)


def _fire_count_path(agent_key: str) -> Path:
    """Return the Path of the temp file tracking hook fires for this agent."""
    return Path(f"/tmp/lobster-auditor-hook-fires-{agent_key}")


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


def _increment_fire_count(hook_input: dict) -> tuple[int, float]:
    """Increment the fire count for this agent and return (new_count, first_fire_ts).

    On the first fire, records the current timestamp as first_fire_ts.
    """
    key = _agent_key(hook_input)
    path = _fire_count_path(key)
    count, first_fire_ts = _read_fire_state(path)

    now = time.time()
    count += 1
    if count == 1:
        first_fire_ts = now

    _write_fire_state(path, count, first_fire_ts)
    return count, first_fire_ts


def _write_circuit_breaker_error(hook_input: dict) -> None:
    """Append a system_error observation to observations.log when the breaker trips.

    Written as a single JSON line matching the observations.log format.
    Best-effort: any failure is silently swallowed so the hook always exits 0.
    """
    try:
        _OBSERVATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        agent_id = hook_input.get("agent_id") or hook_input.get("session_id") or "unknown"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "category": "system_error",
            "task_id": agent_id,
            "text": (
                f"CIRCUIT BREAKER TRIPPED: require-auditor-context-update.py exhausted "
                f"MAX_HOOK_FIRES={MAX_HOOK_FIRES}. Agent exited without completing context "
                f"update. This indicates a serious problem — investigate immediately."
            ),
        }
        with open(_OBSERVATIONS_LOG, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Never block exit on log failure


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        _exit_ok()  # Unreadable input — never block

    # CC 2.1.76+: SubagentStop passes the transcript as a JSONL file at
    # agent_transcript_path rather than inline. Load from the file path,
    # falling back to the legacy inline key for older CC versions.
    transcript_path = hook_input.get("agent_transcript_path", "")
    if transcript_path:
        transcript = _load_transcript_from_jsonl(transcript_path)
    else:
        transcript = hook_input.get("transcript", [])

    tool_calls = _extract_tool_calls(transcript)

    # Fast path: not an auditor session — pass through.
    if not _is_auditor_session(tool_calls):
        _exit_ok()

    # --- Auditor session detected ---

    # Condition 1: context file was updated during this session.
    session_start = _session_start_time(hook_input, transcript)
    if _context_file_updated_since(session_start):
        # Success path — clean up any fire-count state and allow exit.
        key = _agent_key(hook_input)
        _cleanup_fire_state(_fire_count_path(key))
        _exit_ok()

    # Condition 2: transcript contains the explicit safe word.
    if _safe_word_in_transcript(tool_calls):
        # Success path — clean up any fire-count state and allow exit.
        key = _agent_key(hook_input)
        _cleanup_fire_state(_fire_count_path(key))
        _exit_ok()

    # Neither condition met — increment fire count and check circuit breaker.
    fire_count, _first_fire_ts = _increment_fire_count(hook_input)

    if fire_count >= MAX_HOOK_FIRES:
        # Circuit breaker tripped: log a loud error and allow exit.
        # Continuing to block after MAX fires would trap the agent forever.
        _write_circuit_breaker_error(hook_input)
        key = _agent_key(hook_input)
        _cleanup_fire_state(_fire_count_path(key))
        _exit_ok()

    # Still within the retry window — block exit with a reminder.
    fires_remaining = MAX_HOOK_FIRES - fire_count
    print(
        "Error: lobster-auditor session ended without updating "
        "system-audit.context.md. "
        "Either update the file with your findings, or include "
        f"{SAFE_WORD!r} as the first line of your write_result call "
        "if nothing new was found. "
        f"({fires_remaining} attempt(s) remaining before the circuit breaker trips.)",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
