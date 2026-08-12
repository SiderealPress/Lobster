#!/usr/bin/env python3
"""
Soft-warn guard: flags a dispatcher deferral reply (e.g. "I'll check on that")
that is not followed by the required `create_task(subject="DEFERRED: ...")`
call, per the "Commitment Durability" contract in
`.claude/sys.dispatcher.bootup.md`.

## Why this exists

That instruction has zero code-level enforcement today: nothing checks that
a `create_task` call with a `DEFERRED:` subject actually follows a deferral
reply. If the dispatcher forgets the follow-up call, the commitment is
silently and permanently lost -- no trace, no alert. See GitHub issue #2199.

## Design

Single script, registered for two hook events (branches on
`hook_event_name` in the stdin payload):

1. **PostToolUse, matcher `mcp__lobster-inbox__send_reply`:** if the reply
   text matches deferral language (see `_DEFERRAL_PATTERNS`) and this is the
   dispatcher session, write a sentinel file
   (`$LOBSTER_WORKSPACE/data/deferred-commitment-pending.json`) recording a
   timestamp and a short snippet of the reply.

2. **PostToolUse, matcher `mcp__lobster-inbox__create_task`:** if the task
   subject starts with `DEFERRED:`, clear the sentinel -- the commitment was
   captured as required.

3. **PreToolUse, matcher `""` (all tools):** if the sentinel exists and the
   *next* tool call is anything other than `create_task`, this means a
   deferral reply was sent and the dispatcher moved on to something else
   without creating the tracking task. Print a soft warning to stderr and
   clear the sentinel (so the warning fires exactly once per miss, not on
   every subsequent tool call). This never blocks (`exit(0)` always) --
   deferral-language detection is a natural-language heuristic with a real
   false-positive rate, so hard-blocking would risk deadlocking legitimate
   dispatcher work.

## Fail-open policy

Any error reading/writing the sentinel file, parsing stdin, or determining
session role results in `exit(0)` with no warning. This hook must never
block or crash the dispatcher due to its own infrastructure failures.

## Settings.json configuration

Add to hooks -> PostToolUse:

    {
      "matcher": "mcp__lobster-inbox__send_reply|mcp__lobster-inbox__create_task",
      "hooks": [
        {
          "type": "command",
          "command": "python3 $HOME/lobster/hooks/deferred-commitment-guard.py",
          "timeout": 5
        }
      ]
    }

Add to hooks -> PreToolUse:

    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "python3 $HOME/lobster/hooks/deferred-commitment-guard.py",
          "timeout": 5
        }
      ]
    }
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Make hooks/ importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).parent))

from session_role import is_dispatcher_session  # noqa: E402 -- after sys.path insertion

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
SENTINEL_PATH = _WORKSPACE / "data" / "deferred-commitment-pending.json"

SEND_REPLY_TOOL = "mcp__lobster-inbox__send_reply"
CREATE_TASK_TOOL = "mcp__lobster-inbox__create_task"

# Deferral language patterns -- deliberately conservative (favor missed
# detections over false positives, since this hook only soft-warns anyway).
_DEFERRAL_PATTERNS = [
    r"\bi'?ll (check|look into|get back to you|dig into|follow up)\b",
    r"\bi need to (check|look into|dig into)\b",
    r"\bchecking (on|into) (that|this) now\b",
    r"\blet me (check|look into|dig into) (that|this)\b",
    r"\bi'?ll (have an? (answer|update) for you|circle back)\b",
]
_DEFERRAL_RE = re.compile("|".join(_DEFERRAL_PATTERNS), re.IGNORECASE)

_SNIPPET_LEN = 200


# ---------------------------------------------------------------------------
# Sentinel helpers
# ---------------------------------------------------------------------------

def _write_sentinel(reply_text: str) -> None:
    try:
        SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reply_snippet": reply_text[:_SNIPPET_LEN],
        }
        SENTINEL_PATH.write_text(json.dumps(payload))
    except OSError:
        pass  # fail open


def _clear_sentinel() -> None:
    try:
        SENTINEL_PATH.unlink(missing_ok=True)
    except OSError:
        pass  # fail open


def _read_sentinel() -> dict | None:
    try:
        if not SENTINEL_PATH.exists():
            return None
        return json.loads(SENTINEL_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _handle_post_send_reply(data: dict) -> None:
    if not is_dispatcher_session(data):
        return
    tool_input = data.get("tool_input", {})
    text = str(tool_input.get("text", ""))
    if _DEFERRAL_RE.search(text):
        _write_sentinel(text)


def _handle_post_create_task(data: dict) -> None:
    tool_input = data.get("tool_input", {})
    subject = str(tool_input.get("subject", ""))
    if subject.strip().startswith("DEFERRED:"):
        _clear_sentinel()


def _handle_pre_tool(data: dict) -> None:
    if data.get("agent_id"):
        return  # subagent -- this guard only tracks dispatcher commitments
    sentinel = _read_sentinel()
    if sentinel is None:
        return

    tool_name = data.get("tool_name", "")
    if tool_name == CREATE_TASK_TOOL:
        # Give the upcoming create_task call a chance to be the DEFERRED:
        # follow-up; _handle_post_create_task clears the sentinel if so.
        return

    # Any other next tool call means the required create_task call did not
    # immediately follow the deferral reply. Warn once, then clear so this
    # doesn't repeat on every subsequent tool call for the same miss.
    snippet = sentinel.get("reply_snippet", "")
    print(
        "WARNING [deferred-commitment-guard]: the last reply looked like a "
        f"deferral (\"{snippet}\") but no create_task(subject=\"DEFERRED: ...\") "
        "call followed. If this was a real commitment, create the tracking "
        "task now (see 'Commitment Durability' in sys.dispatcher.bootup.md).",
        file=sys.stderr,
    )
    _clear_sentinel()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    event = data.get("hook_event_name", "")
    tool_name = data.get("tool_name", "")

    try:
        if event == "PostToolUse" and tool_name == SEND_REPLY_TOOL:
            _handle_post_send_reply(data)
        elif event == "PostToolUse" and tool_name == CREATE_TASK_TOOL:
            _handle_post_create_task(data)
        elif event == "PreToolUse":
            _handle_pre_tool(data)
    except Exception:  # noqa: BLE001 -- never let this hook crash the session
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
