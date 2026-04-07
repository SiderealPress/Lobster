#!/usr/bin/env python3
"""
PostToolUse hook: context window guard.

Below the warning threshold (default 70%): logs usage to context-monitor.log.
At or above the threshold: writes a context_warning message to ~/messages/inbox/
so the dispatcher can enter wind-down mode and trigger a graceful restart.

Dedup: once a warning is written, /tmp/lobster-context-warning-sent is touched
so subsequent hook firings skip the write until the flag is cleared on restart.

Issue #1430: Claude Code does NOT populate context_window in PostToolUse payloads
for MCP tool calls — only for built-in tools like Bash.  Previously the hook
silently returned when context_window was None, making "hook fired, no data"
indistinguishable from "hook never fired."

Fix: when context_window is absent, log a WARN entry so the log records that the
hook fired.  The hook matcher in settings.json was also broadened to include Bash
(which does carry context_window), ensuring real usage data is captured.
"""
import json
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

WARNING_THRESHOLD = 70.0
DEDUP_FLAG = Path("/tmp/lobster-context-warning-sent")


def _log_usage(log_dir: Path, entry: dict) -> None:
    """Append a usage entry to the context-monitor log."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "context-monitor.log"
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _build_log_entry(tool_name: str, used_pct: float, remaining_pct: float, context: dict) -> dict:
    """Return an immutable log entry dict from raw context data."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "used_percentage": used_pct,
        "remaining_percentage": remaining_pct,
        "full_context_window": context,
    }


def _build_absent_context_entry(tool_name: str) -> dict:
    """Return a WARN log entry for when context_window is absent from the payload.

    This makes 'hook fired, no data' distinguishable from 'hook never fired'.
    Claude Code only populates context_window for built-in tools (e.g. Bash),
    not for MCP tool calls.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "context_window_absent": True,
        "warn": f"[WARN] context_window absent in payload for tool: {tool_name}",
    }


def _build_warning_message(used_pct: float) -> dict:
    """Return the context_warning inbox message payload."""
    return {
        "id": str(uuid.uuid4()),
        "type": "context_warning",
        "source": "system",
        "chat_id": 0,
        "text": f"Context window at {used_pct:.1f}% — entering wind-down mode",
        "used_percentage": used_pct,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _write_warning_to_inbox(inbox_dir: Path, message: dict) -> None:
    """Write the warning message JSON to the inbox directory."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    filename = f"context-warning-{message['id']}.json"
    (inbox_dir / filename).write_text(json.dumps(message, indent=2))


def _handle_payload(
    data: dict,
    log_dir: Path | None = None,
    inbox_dir: Path | None = None,
) -> None:
    """Process a single PostToolUse payload.

    Accepts log_dir and inbox_dir as injectable parameters so tests can verify
    behavior without touching the real filesystem.  When not provided, defaults
    to the standard runtime paths.
    """
    if log_dir is None:
        log_dir = Path.home() / "lobster-workspace" / "logs"
    if inbox_dir is None:
        inbox_dir = Path.home() / "messages" / "inbox"

    tool_name = data.get("tool_name", "unknown")
    context = data.get("context_window")

    if context is None:
        # Hook fired but Claude Code did not provide context_window data.
        # Log a WARN entry so the log shows the hook is running even when no
        # usage data is available (distinguishes "no data" from "not firing").
        entry = _build_absent_context_entry(tool_name)
        _log_usage(log_dir, entry)
        return

    used_pct = context.get("used_percentage")
    remaining_pct = context.get("remaining_percentage")

    if used_pct is None:
        entry = _build_absent_context_entry(tool_name)
        _log_usage(log_dir, entry)
        return

    entry = _build_log_entry(tool_name, used_pct, remaining_pct, context)
    _log_usage(log_dir, entry)

    if used_pct < WARNING_THRESHOLD:
        return

    # At or above threshold — write a context_warning to inbox (once per session)
    if DEDUP_FLAG.exists():
        return

    message = _build_warning_message(used_pct)
    _write_warning_to_inbox(inbox_dir, message)
    DEDUP_FLAG.touch()


def main() -> None:
    try:
        data = json.load(sys.stdin)
        _handle_payload(data)
    except Exception:
        pass  # Never block tool use


if __name__ == "__main__":
    main()
