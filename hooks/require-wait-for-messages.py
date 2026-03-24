#!/usr/bin/env python3
"""
Stop hook: ensure the dispatcher calls wait_for_messages before ending a turn.

The dispatcher's main loop is: process messages → call wait_for_messages →
repeat. When the dispatcher stalls — typically after processing a batch of
subagent results — it can end a turn without calling wait_for_messages. The
health check catches this after ~12 minutes and restarts, but that is a long
window with missed messages.

This hook fires on every Stop event (dispatcher or subagent). Subagent sessions
are immediately exempted via session_role.is_dispatcher(). For the dispatcher,
the hook scans the transcript: if wait_for_messages was not called, it exits
with code 2 and injects a reminder into the next Claude turn.

## Transcript handling

Stop hooks in CC 2.1.76+ pass a file path (transcript_path) rather than an
inline transcript list. Both the file-based and legacy inline forms are tried
so the hook works across CC versions.

Each line of the JSONL transcript file has the structure:
    {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

Tool use items are nested under entry["message"]["content"], not entry["content"].
_collect_tool_names() handles both JSONL and legacy inline formats.

## Suppressing feedback injection on success

Claude Code injects a "Stop hook feedback: ... No stderr output" system message
even when the hook exits 0. To prevent this from triggering a new turn,
the hook outputs JSON with {"suppressOutput": true} on all success paths.

## Exemptions

The hook does NOT fire for:
- Subagent sessions (is_dispatcher() returns False)
- Any session where wait_for_messages was called at least once in the transcript
"""
import json
import sys
from pathlib import Path

# Import shared session role utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher

# JSON to emit on every successful (allow) exit — suppresses the
# "Stop hook feedback: No stderr output" injection that CC 2.1.76+ produces
# even when the hook exits 0 with no output.
_SILENT_OK = json.dumps({"suppressOutput": True})

_WFM_TOOL = "mcp__lobster-inbox__wait_for_messages"

_REMINDER = (
    "Dispatcher: you ended this turn without calling wait_for_messages. "
    "Call it now to continue the main loop."
)


def _exit_ok() -> None:
    """Exit 0 with JSON that suppresses CC feedback injection."""
    print(_SILENT_OK)
    sys.exit(0)


def _load_transcript_from_jsonl(path: str) -> list:
    """Load transcript entries from a JSONL file. Returns [] on any error."""
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


def _collect_tool_names(transcript: list) -> list[str]:
    """Walk transcript entries and return names of all tool_use blocks.

    Handles both JSONL format (CC 2.1.76+) and legacy inline format:

    JSONL:   {"type": "assistant", "message": {"content": [...]}, ...}
    Legacy:  {"role": "assistant", "content": [...]}
    """
    names: list[str] = []
    for entry in transcript:
        if not isinstance(entry, dict):
            continue
        nested_msg = entry.get("message")
        if isinstance(nested_msg, dict):
            content = nested_msg.get("content", [])
        else:
            content = entry.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                name = item.get("name", "")
                if name:
                    names.append(name)
    return names


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        _exit_ok()  # If we can't read input, don't block.

    # Only enforce on the dispatcher session.
    if not is_dispatcher(data):
        _exit_ok()

    # Load transcript: prefer file-based path (CC 2.1.76+), fall back to inline.
    transcript_path = data.get("transcript_path", "")
    if transcript_path:
        transcript = _load_transcript_from_jsonl(transcript_path)
    else:
        transcript = data.get("transcript", [])

    tool_names = _collect_tool_names(transcript)

    if _WFM_TOOL in tool_names:
        _exit_ok()

    # wait_for_messages was not called — inject a reminder.
    print(_REMINDER, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
