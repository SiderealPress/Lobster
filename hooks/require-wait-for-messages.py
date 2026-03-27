#!/usr/bin/env python3
"""
Stop hook: block the dispatcher from ending a turn without calling wait_for_messages.

The dispatcher's main loop is: process messages → call wait_for_messages →
repeat. When the dispatcher stalls — typically after processing a batch of
subagent results or system messages — it can end a turn without calling
wait_for_messages. The health check catches this after ~12 minutes and
restarts, causing ~30s of missed messages per incident.

This hook fires on every Stop event (dispatcher or subagent). Subagent
sessions are immediately exempted via session_role.is_dispatcher(). For the
dispatcher, the hook scans the transcript: if wait_for_messages was not
called, it exits 2 (blocking) so the dispatcher MUST call wait_for_messages
before the turn ends.

## Why blocking (exit 2) instead of warn-only (exit 0)

The warn-only version (PR #815) printed to stderr but Claude proceeded with
the turn ending anyway — 3 restarts occurred tonight after that PR merged.
The warning fires after the turn is already ending; Claude has no chance to
act on it in the same turn. Exit 2 injects the error message as a system
turn and Claude gets a new turn where it must call wait_for_messages before
stopping again.

This mirrors how require-write-result.py handles subagents: hard-block until
the required tool is called.

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

## Graceful exit bypass

Two legitimate cases require the dispatcher to exit without calling
wait_for_messages again:

1. **Hibernation**: wait_for_messages(hibernate_on_timeout=True) returned a
   message containing "Hibernating" or "EXIT" — the dispatcher must exit. Calling
   WFM again would be wrong.
2. **Context restart**: After writing the handoff and calling `lobster restart`,
   the dispatcher should exit. It already called WFM earlier in the same session;
   forcing another call would cause a loop.

In these cases, the dispatcher writes an empty file at /tmp/lobster-graceful-exit
immediately before exiting. The hook checks for this file: if present, it deletes
the file and exits 0 (allowing the turn to end). The file is consumed on first use.

The block message itself documents this bypass mechanism so the dispatcher can act
on it without consulting external docs.

## Exemptions

The hook does NOT fire for:
- Sessions without LOBSTER_MAIN_SESSION=1 (non-Lobster Claude Code sessions)
- Subagent sessions (is_dispatcher() returns False)
- Any session where wait_for_messages was called at least once in the transcript
- Transcript read failures (I/O error → allow stop rather than false-positive block)
- Graceful exit bypass: /tmp/lobster-graceful-exit file present (consumed on use)
"""
import json
import os
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

_GRACEFUL_EXIT_FLAG = "/tmp/lobster-graceful-exit"

_BLOCK_MESSAGE = (
    "BLOCKED: You are the Lobster dispatcher. You ended this turn without calling "
    "wait_for_messages. The main loop requires wait_for_messages as the very next "
    "action after mark_processed — no deliberation, no state assessment.\n\n"
    "Call mcp__lobster-inbox__wait_for_messages NOW. Do not do anything else first.\n\n"
    "If this is a legitimate graceful exit (hibernation complete or context restart), "
    "write the bypass flag and then exit:\n"
    f"    open({_GRACEFUL_EXIT_FLAG!r}, 'w').close()"
)


def _exit_ok() -> None:
    """Exit 0 with JSON that suppresses CC feedback injection."""
    print(_SILENT_OK)
    sys.exit(0)


def _load_transcript_from_jsonl(path: str) -> list | None:
    """Load transcript entries from a JSONL file.

    Returns a list of entries on success (may be empty if the file is empty),
    or None on any I/O or OS error (distinguishes read failure from empty file).
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
        return None


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
    # Only run for sessions started by Lobster (LOBSTER_MAIN_SESSION=1).
    # This guards against firing in a developer's personal Claude Code session.
    if os.environ.get("LOBSTER_MAIN_SESSION", "") != "1":
        _exit_ok()

    try:
        data = json.load(sys.stdin)
    except Exception:
        _exit_ok()  # If we can't read input, don't block.

    # Only enforce for the dispatcher session.
    if not is_dispatcher(data):
        _exit_ok()

    # Load transcript: prefer file-based path (CC 2.1.76+), fall back to inline.
    transcript_path = data.get("transcript_path", "")
    if transcript_path:
        transcript = _load_transcript_from_jsonl(transcript_path)
        if transcript is None:
            # I/O error reading the transcript — can't determine whether
            # wait_for_messages was called. Allow stop rather than false-positive
            # block; the health check remains the backstop.
            print(
                "[require-wait-for-messages] WARNING: could not read transcript "
                f"at {transcript_path!r} — skipping wait_for_messages check.",
                file=sys.stderr,
            )
            _exit_ok()
    else:
        transcript = data.get("transcript", [])

    tool_names = _collect_tool_names(transcript)

    if _WFM_TOOL in tool_names:
        _exit_ok()

    # wait_for_messages was not called. Check for the graceful exit bypass before
    # blocking — the dispatcher writes this flag when intentionally exiting without
    # WFM (hibernation complete, context restart).
    flag = Path(_GRACEFUL_EXIT_FLAG)
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass  # Already deleted by a concurrent process — still honour the bypass.
        _exit_ok()

    # No bypass flag — block the stop (exit 2). Claude gets a new turn with
    # _BLOCK_MESSAGE injected and must call wait_for_messages (or write the bypass
    # flag) before the next stop attempt succeeds.
    print(_BLOCK_MESSAGE, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
