#!/usr/bin/env python3
"""
Stop / SubagentStop hook: log session exits to observations.log.

Records every session exit (graceful or unexpected) as a structured JSON line
in ~/lobster-workspace/logs/observations.log with category "session_exit".
The log entry includes:

  - session_id      — the Claude Code session UUID (or agent_id for subagents)
  - hook_event      — "Stop" or "SubagentStop"
  - exit_cause      — derived from the transcript; one of:
                        "end_turn"         normal completion
                        "write_result"     subagent delivered result
                        "potential_overflow"  message count > OVERFLOW_THRESHOLD
                        "unknown"          could not determine
  - message_count   — number of turns in the transcript (proxy for context depth)
  - is_dispatcher   — bool; True if this is the main dispatcher session

This hook is read-only and always exits 0 (never blocks the session from exiting).
It is a passive observer only — no blocking, no side effects beyond the log write.

## Overflow detection

If the session exited without calling write_result AND the transcript message
count exceeds OVERFLOW_THRESHOLD (200), the exit_cause is tagged as
"potential_overflow". This is a heuristic for data collection before confirmed
overflow crashes — not a guarantee.

## Log format

Each line is a JSON object appended to observations.log, compatible with the
existing log consumers. Example:

  {"ts": "2026-05-07T12:34:56Z", "category": "session_exit", "session_id": "...",
   "hook_event": "SubagentStop", "exit_cause": "write_result",
   "message_count": 42, "is_dispatcher": false}
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add hooks dir to sys.path so session_role can be imported.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher, get_session_id

# Number of transcript messages above which we tag exit as "potential_overflow".
# Matches the threshold used in context-monitor.py for consistency.
OVERFLOW_THRESHOLD = 200

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_OBS_LOG = _WORKSPACE / "logs" / "observations.log"


def _count_transcript_messages(data: dict) -> int:
    """Return the number of messages in the transcript, or 0 on any error.

    Handles both:
      - Stop:        transcript_path (JSONL file, one JSON object per line)
      - SubagentStop: agent_transcript_path (JSONL file)
      - Legacy:      inline transcript list (older CC versions)
    """
    hook_event = data.get("hook_event_name", "")
    is_subagentstop = hook_event == "SubagentStop"

    if is_subagentstop:
        path_str = data.get("agent_transcript_path", "")
    else:
        path_str = data.get("transcript_path", "")

    if path_str:
        try:
            path = Path(path_str)
            if path.exists():
                lines = [line for line in path.read_text().splitlines() if line.strip()]
                return len(lines)
        except Exception:
            pass
        return 0

    # Legacy inline transcript fallback
    transcript = data.get("transcript", [])
    if isinstance(transcript, list):
        return len(transcript)
    return 0


def _has_write_result(data: dict) -> bool:
    """Return True if write_result was called in the transcript.

    Scans the JSONL transcript file (or inline transcript list) for any
    tool_use item with name == 'mcp__lobster-inbox__write_result'.
    """
    hook_event = data.get("hook_event_name", "")
    is_subagentstop = hook_event == "SubagentStop"

    if is_subagentstop:
        path_str = data.get("agent_transcript_path", "")
    else:
        path_str = data.get("transcript_path", "")

    if path_str:
        try:
            path = Path(path_str)
            if path.exists():
                for line in path.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    content = (
                        entry.get("message", {}).get("content", [])
                        if "message" in entry
                        else entry.get("content", [])
                    )
                    if isinstance(content, list):
                        for item in content:
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "tool_use"
                                and item.get("name") == "mcp__lobster-inbox__write_result"
                            ):
                                return True
        except Exception:
            pass
        return False

    # Legacy inline transcript
    transcript = data.get("transcript", [])
    if isinstance(transcript, list):
        for entry in transcript:
            content = (
                entry.get("message", {}).get("content", [])
                if "message" in entry
                else entry.get("content", [])
            )
            if isinstance(content, list):
                for item in content:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "tool_use"
                        and item.get("name") == "mcp__lobster-inbox__write_result"
                    ):
                        return True
    return False


def _derive_exit_cause(data: dict, is_disp: bool, message_count: int) -> str:
    """Derive the exit_cause label from hook data and transcript content.

    Priority:
      1. Dispatcher sessions → "end_turn" (they don't call write_result)
      2. Subagents that called write_result → "write_result"
      3. High message count (> OVERFLOW_THRESHOLD) without write_result → "potential_overflow"
      4. Everything else → "unknown"
    """
    if is_disp:
        return "end_turn"
    if _has_write_result(data):
        return "write_result"
    if message_count > OVERFLOW_THRESHOLD:
        return "potential_overflow"
    return "unknown"


def _append_to_observations_log(record: dict) -> None:
    """Append a JSON line to observations.log. Best-effort: swallows any I/O error."""
    try:
        _OBS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _OBS_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        # Can't parse input — exit silently, never block the session.
        sys.exit(0)

    hook_event = data.get("hook_event_name", "Stop")
    session_id = (
        data.get("agent_id")
        or get_session_id(data)
        or "unknown"
    )
    is_disp = is_dispatcher(data)
    message_count = _count_transcript_messages(data)
    exit_cause = _derive_exit_cause(data, is_disp, message_count)

    now_iso = datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat()

    record = {
        "ts": now_iso,
        "category": "session_exit",
        "session_id": session_id,
        "hook_event": hook_event,
        "exit_cause": exit_cause,
        "message_count": message_count,
        "is_dispatcher": is_disp,
        "source": "session-exit-logger",
    }

    _append_to_observations_log(record)

    # Always exit 0 — this hook is read-only and must never block exit.
    # Emit suppressOutput=True to prevent the "No stderr output" feedback injection.
    print(json.dumps({"suppressOutput": True}))
    sys.exit(0)


if __name__ == "__main__":
    main()
