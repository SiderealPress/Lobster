#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent tool calls where the prompt doesn't contain a task_id.

The task_id must appear in the prompt so the subagent can pass it to write_result.
This enables reliable DB matching in the SubagentStop hook, which extracts task_id
from write_result args in the transcript.

This is the spawn-side mirror of require-register-agent-task-id.py: that hook
blocks register_agent calls without task_id; this hook blocks Agent spawns where
the prompt doesn't carry task_id into the subagent's context at all.

## Accepted formats

YAML frontmatter (preferred):

    ---
    task_id: my-task
    chat_id: ADMIN_CHAT_ID_REDACTED
    source: telegram
    ---

Legacy text format (backward compat, still accepted):

    Your task_id is: my-task

## Frontmatter completeness (chat_id, source, task_origin)

Once task_id is present, the frontmatter is also checked for `chat_id` and
`source`; `chat_id: 0` is accepted only with `task_origin: scheduled` or
`task_origin: internal`. LOBSTER_SPAWN_FRONTMATTER_MODE selects the response:

- `warn` (default, also used for unknown values): append one line to the warn
  log (LOBSTER_SPAWN_FRONTMATTER_WARN_LOG, default
  ~/lobster-workspace/logs/spawn-frontmatter-warn.log), print a note to
  stderr, allow the spawn.
- `block`: exit 2 with a message naming the key(s) to add.

The decision logic is pure and lives in spawn_frontmatter.py; this file does
the I/O. A missing task_id is always blocked, independent of the mode.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def _has_task_id_in_frontmatter(prompt: str) -> bool:
    """Return True if prompt starts with YAML frontmatter containing a task_id key."""
    prompt = prompt.lstrip()
    if not prompt.startswith("---"):
        return False

    rest = prompt[3:]
    end = rest.find("\n---")
    if end == -1:
        return False

    block = rest[:end]
    for line in block.splitlines():
        line = line.strip()
        if re.match(r"^task_id\s*:", line):
            # Check the value is non-empty
            _, _, value = line.partition(":")
            if value.strip():
                return True

    return False


def _has_task_id_in_text(prompt: str) -> bool:
    """Return True if prompt contains the legacy 'task_id is: X' pattern."""
    return bool(re.search(r"task_id\s+is:\s*\S+", prompt, re.IGNORECASE))


def _check_spawn_frontmatter(prompt: str, subagent_type: str | None) -> int:
    """Apply the chat_id/source/task_origin check; return the hook exit code.

    I/O boundary for spawn_frontmatter's pure decision. Fails open (0) if the
    helper cannot be loaded or the warn log cannot be written.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import spawn_frontmatter as sf
    except Exception as e:  # noqa: BLE001
        print(f"Warning: spawn frontmatter check unavailable: {e}", file=sys.stderr)
        return 0

    check = sf.evaluate_spawn(prompt, sf.resolve_mode(os.environ.get(sf.MODE_ENV)))
    if check.blocked:
        print(sf.format_block_message(check.problems), file=sys.stderr)
        return 2
    if check.warn:
        try:
            log_path = sf.resolve_warn_log_path(os.environ, Path.home())
            log_path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with log_path.open("a") as f:
                f.write(sf.format_warn_line(ts, subagent_type, check.problems) + "\n")
        except Exception:  # noqa: BLE001
            pass  # Logging must never block a spawn in warn mode
        print(sf.format_warn_message(check.problems), file=sys.stderr)
    return 0


try:
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {})
except Exception as e:
    print(f"Warning: require-task-id-in-prompt hook received malformed input: {e}", file=sys.stderr)
    sys.exit(0)

if tool != "Agent":
    sys.exit(0)

prompt = inp.get("prompt", "")

if _has_task_id_in_frontmatter(prompt) or _has_task_id_in_text(prompt):
    sys.exit(_check_spawn_frontmatter(prompt, inp.get("subagent_type")))

print(
    'BLOCKED: Agent spawned without task_id in prompt. '
    'Include a YAML frontmatter block (---\\ntask_id: <slug>\\n---) at the top of the prompt, '
    'or the legacy format "Your task_id is: <slug>". '
    'This enables reliable DB matching in the SubagentStop hook.',
    file=sys.stderr,
)
sys.exit(2)
