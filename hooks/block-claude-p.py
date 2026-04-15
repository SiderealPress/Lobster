#!/usr/bin/env python3
r"""
PreToolUse hook: detect and log/block `claude -p` / `claude --print` Bash invocations.

Background: `claude -p` (non-interactive print mode) is sometimes used as a
lightweight LLM call from shell scripts. In Lobster, this pattern is almost always
a mistake — it spawns a new Claude instance inline, consumes tokens at unpredictable
cost, and bypasses the structured subagent dispatch that write_result enforces.
Known-safe callers (run-job.sh, claude-persistent.sh) are explicitly allowlisted.

This hook operates in two modes controlled by LOBSTER_BLOCK_CLAUDE_P_MODE:
  warn  (default) — log the match and allow the tool call through. Zero production
                    impact; run for 24h to validate before switching to block.
  block           — hard-block the tool call with exit 2 and a remediation message.

Scope: Bash tool only. Write and Edit tool calls are ignored entirely.

Pattern: ``claude\s+(-p|--print)\s`` — intentionally conservative. Matches the
executable name followed by the short or long flag followed by at least one space
(ensuring it's used as an argument separator, not a flag to something else).

Allowlist (do not fire on):
  - Lines starting with `#` (shell comments — not executable)
  - Known-safe script names: `run-job.sh`, `claude-persistent.sh` (callers that
    legitimately invoke claude -p as part of the Lobster job runner infrastructure)
  - Strings where the match is clearly inside a quoted string assignment (heuristic:
    pattern appears after `="` or `='` without being followed by `"` or `'` close)

Logging: matches are appended to ~/lobster-workspace/logs/claude-p-blocks.jsonl
as JSON objects with timestamp, mode, command (first 500 chars), match, and
allowlist_hit fields.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Mode: 'warn' (log + allow) | 'block' (log + reject)
# Default is 'warn' — safe for Phase 1 deployment.
_MODE = os.environ.get("LOBSTER_BLOCK_CLAUDE_P_MODE", "warn").strip().lower()

# Pattern: `claude` followed by optional flags, then `-p` or `--print`, then whitespace.
# Uses word-boundary-style matching: `claude` must be at word start (start of string,
# space, or after `&|;(` shell metacharacters) to avoid false-positives on longer paths.
_CLAUDE_P_PATTERN = re.compile(
    r"""(?:^|[\s&|;(])claude\s+(?:[^\s]*\s+)*(?:-p|--print)\s""",
    re.MULTILINE,
)

# Known-safe callers whose names appear in the command string.
# These are Lobster infrastructure scripts that intentionally use claude -p.
_SAFE_CALLERS = frozenset([
    "run-job.sh",
    "claude-persistent.sh",
])

# Log file for matches (append-only JSONL).
_LOG_FILE = Path(os.path.expanduser("~/lobster-workspace/logs/claude-p-blocks.jsonl"))


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

def _is_comment_line(line: str) -> bool:
    """Return True if the line is a shell comment (starts with optional whitespace + #)."""
    return line.lstrip().startswith("#")


def _contains_safe_caller(command: str) -> bool:
    """Return True if the command invokes a known-safe caller script."""
    return any(caller in command for caller in _SAFE_CALLERS)


def _is_string_literal_context(command: str, match: re.Match) -> bool:
    """Heuristic: return True if the match appears to be inside a quoted string assignment.

    Checks whether the text immediately before the match starts contains an open
    quote character (`="` or `='`) without a matching close quote before the match
    position — a strong signal that the command is being assigned, not executed.

    This is intentionally conservative: when in doubt, we DO fire (false positives
    are safe in warn mode; in block mode the allowlist is the safety net).
    """
    prefix = command[: match.start()]
    # Look for an unclosed string assignment immediately preceding the match.
    # Pattern: ...="... (no closing " before the match) — indicates assignment context.
    for quote_char in ('"', "'"):
        assign_pat = re.compile(r'={}[^{}]*$'.format(re.escape(quote_char), re.escape(quote_char)))
        if assign_pat.search(prefix):
            return True
    return False


def _find_matches_in_command(command: str) -> list[str]:
    """Return list of matched substrings in command that are NOT allowlisted.

    Each element is the matched text. An empty list means no actionable matches.
    """
    results = []
    for m in _CLAUDE_P_PATTERN.finditer(command):
        # Check each line at the match position.
        line_start = command.rfind("\n", 0, m.start()) + 1
        line_end_idx = command.find("\n", m.start())
        line = command[line_start:] if line_end_idx == -1 else command[line_start:line_end_idx]

        if _is_comment_line(line):
            continue  # Shell comment — not executed
        if _contains_safe_caller(command):
            continue  # Known-safe caller
        if _is_string_literal_context(command, m):
            continue  # Likely an assignment, not an invocation

        results.append(m.group(0).strip())
    return results


def _append_log(entry: dict) -> None:
    """Append a JSON entry to the JSONL log file. Best-effort: never raises."""
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Log failures must never block the tool call


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # Can't parse input — don't block

    # Scope: Bash tool only.
    tool_name = data.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "") or ""

    matches = _find_matches_in_command(command)
    if not matches:
        sys.exit(0)  # No actionable matches

    # There is at least one non-allowlisted match. Log it.
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": _MODE,
        "tool": tool_name,
        "command_prefix": command[:500],
        "matches": matches,
    }
    _append_log(entry)

    if _MODE == "block":
        print(
            "BLOCKED: The Bash command appears to invoke `claude -p` / `claude --print` "
            "in a non-interactive context. This spawns a new Claude instance inline, "
            "bypassing structured subagent dispatch and write_result delivery.\n\n"
            "If you need to run an LLM task, spawn a background subagent via the Task tool "
            "and call write_result when done. If this invocation is intentional and safe "
            "(e.g. a Lobster infrastructure script), ask the operator to add it to the "
            "LOBSTER_BLOCK_CLAUDE_P_MODE allowlist.\n\n"
            f"Matched pattern in command: {matches[0]!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    else:
        # warn mode: emit a soft observation and allow through.
        print(
            f"[block-claude-p warn] Detected `claude -p` in Bash command (mode=warn, not blocked). "
            f"Match: {matches[0]!r}. "
            f"Set LOBSTER_BLOCK_CLAUDE_P_MODE=block to hard-block after validation.",
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
