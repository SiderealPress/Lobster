#!/usr/bin/env python3
"""
PreToolUse / PostToolUse hook: wait_for_messages lifecycle logger.

Writes WFM_ENTER (PreToolUse) and WFM_EXIT (PostToolUse) events to the
session lifecycle log so we can measure WFM blocking duration and identify
the last WFM call before a crash.

## Motivation

On 2026-05-01 a CC dispatcher crash at ~13:40Z left no logged exit reason.
Post-incident investigation could only infer a 3.5-minute WFM block from
log gaps — there was no direct measurement. This hook provides that signal.

## Events written

PreToolUse (matcher: "mcp__lobster-inbox__wait_for_messages"):
    {"ts": "...", "event": "WFM_ENTER", "session_id": "..."}

PostToolUse (matcher: "mcp__lobster-inbox__wait_for_messages"):
    {"ts": "...", "event": "WFM_EXIT", "session_id": "...", "duration_s": 213.4,
     "messages": 2}

## Design

- Dispatcher-only: subagent sessions are skipped via session_role.
- Module-level references to is_dispatcher and is_dispatcher_session allow
  tests to monkeypatch them cleanly without importing session_role directly.
- Append-only JSONL: one JSON object per line, never overwrites existing entries.
- Atomic append: open with O_APPEND — writes <= PIPE_BUF (4096 bytes) are atomic
  on Linux for single-writer use.
- Silent failure: never blocks tool execution.

## Usage

This single script handles both PreToolUse and PostToolUse by reading
WFM_HOOK_TYPE from environment: "pre" → WFM_ENTER, "post" → WFM_EXIT.

The settings.json wiring uses two separate entries with different hook commands
that set WFM_HOOK_TYPE before invoking this script.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: add hooks dir to sys.path so session_role is importable.
# Must happen before session_role import below.
# ---------------------------------------------------------------------------
_HOOKS_DIR = Path(__file__).parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from session_role import is_dispatcher_session  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WORKSPACE_DIR = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
LIFECYCLE_LOG = WORKSPACE_DIR / "logs" / "session-lifecycle.log"

# Environment variable set by the settings.json hook wrapper to distinguish
# PreToolUse ("pre") from PostToolUse ("post") invocations.
HOOK_TYPE = os.environ.get("WFM_HOOK_TYPE", "")

# State file: records WFM entry epoch so PostToolUse can compute duration.
WFM_ENTER_TS_FILE = WORKSPACE_DIR / "logs" / "wfm-enter-ts"


# ---------------------------------------------------------------------------
# Core I/O primitives (pure functions for testability)
# ---------------------------------------------------------------------------

def _ts() -> str:
    """Return current time as ISO 8601 UTC string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_event(event: dict, lifecycle_log: Path = LIFECYCLE_LOG) -> None:
    """Append a JSON event line to the lifecycle log.

    Uses O_APPEND so each write is atomic at the filesystem level for writes
    <= PIPE_BUF (4096 bytes on Linux), which all our events satisfy.
    """
    lifecycle_log.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with open(lifecycle_log, "a", encoding="utf-8") as f:
        f.write(line)


def _write_enter_ts(ts_epoch: float, enter_ts_file: Path = WFM_ENTER_TS_FILE) -> None:
    """Write WFM entry epoch atomically so PostToolUse can compute duration."""
    enter_ts_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = enter_ts_file.with_suffix(".tmp")
    tmp.write_text(str(ts_epoch))
    os.rename(str(tmp), str(enter_ts_file))


def _read_enter_ts(enter_ts_file: Path = WFM_ENTER_TS_FILE) -> "float | None":
    """Return the WFM entry epoch from state file, or None if absent/unreadable."""
    try:
        return float(enter_ts_file.read_text().strip())
    except Exception:
        return None


def _parse_message_count(tool_response: "str | list | None") -> "int | None":
    """Return message count from wait_for_messages tool response, or None."""
    if tool_response is None:
        return None
    if isinstance(tool_response, list):
        return len(tool_response)
    if isinstance(tool_response, str):
        try:
            parsed = json.loads(tool_response)
            if isinstance(parsed, list):
                return len(parsed)
            if isinstance(parsed, dict) and "messages" in parsed:
                return len(parsed["messages"])
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def handle_pre_tool_use(hook_input: dict) -> None:
    """Write WFM_ENTER event and record entry timestamp."""
    if not is_dispatcher_session(hook_input):
        return

    now_epoch = time.time()
    event = {
        "ts": _ts(),
        "event": "WFM_ENTER",
        "session_id": hook_input.get("session_id", ""),
    }
    _append_event(event)
    _write_enter_ts(now_epoch)


def handle_post_tool_use(hook_input: dict) -> None:
    """Write WFM_EXIT event with duration and message count."""
    if not is_dispatcher_session(hook_input):
        return

    now_epoch = time.time()
    enter_epoch = _read_enter_ts()
    duration_s = round(now_epoch - enter_epoch, 1) if enter_epoch is not None else None
    messages = _parse_message_count(hook_input.get("tool_response"))

    event: dict = {
        "ts": _ts(),
        "event": "WFM_EXIT",
        "session_id": hook_input.get("session_id", ""),
    }
    if duration_s is not None:
        event["duration_s"] = duration_s
    if messages is not None:
        event["messages"] = messages

    _append_event(event)

    # Clean up enter timestamp — next WFM will write a fresh one.
    try:
        WFM_ENTER_TS_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        if HOOK_TYPE == "pre":
            handle_pre_tool_use(hook_input)
        elif HOOK_TYPE == "post":
            handle_post_tool_use(hook_input)
        # Unknown HOOK_TYPE: silently do nothing.
    except Exception:
        # Never block tool execution.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
