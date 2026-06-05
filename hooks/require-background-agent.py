#!/usr/bin/env python3
"""PreToolUse hook: blocks the dispatcher from calling Agent without background intent.

The Lobster dispatcher runs in an infinite message-processing loop. A foreground
Agent call blocks the dispatcher for the full duration of the agent — potentially
minutes — while incoming messages queue up unprocessed and health-check pings go
unanswered.

This hook enforces the 7-second rule as a hard constraint for the dispatcher.
Subagents are exempt: they may legitimately spawn nested agents synchronously
when the result is needed to decide the next step.

Note: Claude Code has used both "Agent" and "Task" as the tool name for spawning
subagents across versions. Both are treated identically.

## Background intent signals (either is sufficient)

1. **tool_input["run_in_background"] is True** — the classic signal. Available
   when the Agent tool schema includes the field. In Claude Code 2.1.123+ with
   some model variants, the schema has additionalProperties: false and omits
   run_in_background, causing the client to strip the field before the hook
   sees it. This signal is checked first for backward compatibility.

2. **YAML frontmatter `background: true` in prompt** — the schema-safe workaround
   for issue #1872. The dispatcher includes `background: true` in the YAML
   frontmatter block at the top of the prompt. This survives schema validation
   because it's part of the `prompt` field (which is always present in the schema).

   Example prompt structure:
       ---
       task_id: fix-pr-42
       chat_id: 12345
       source: telegram
       background: true
       ---

       <actual task instructions>

   The hook checks for a line matching `background: true` or `background: True`
   within the frontmatter block (case-insensitive on the value).

Exit codes:
  0 — tool is not Agent/Task, background intent is signalled, or session is a subagent
  2 — hard block: dispatcher called Agent/Task without background intent
"""
import json
import re
import sys
from pathlib import Path

# Import the shared dispatcher/subagent detection utility.
sys.path.insert(0, str(Path(__file__).parent))
from session_role import is_dispatcher

# Tool names used to spawn subagents across CC versions.
AGENT_TOOL_NAMES = {"Agent", "Task"}

# Values accepted as truthy for `background:` in the YAML frontmatter.
# Covers both YAML true/false and Python-style True/False that Claude often writes.
_BACKGROUND_TRUE_VALUES = frozenset({"true", "yes", "1"})


def _has_background_true_in_frontmatter(prompt: str) -> bool:
    """Return True if the prompt has YAML frontmatter with `background: true`.

    Accepts both YAML-style `true` and Python-style `True` (case-insensitive).
    Returns False if:
    - No frontmatter block is present
    - The frontmatter has no `background` key
    - The `background` value is anything other than a truthy string

    A frontmatter block is the `---` ... `---` section at the start of the prompt
    (after stripping leading whitespace).
    """
    prompt = prompt.lstrip()
    if not prompt.startswith("---"):
        return False

    # Find the closing --- of the frontmatter block.
    rest = prompt[3:]
    # Match closing delimiter: must be followed by newline or end-of-string
    m = re.search(r"\n---(?:\n|$)", rest)
    if m is None:
        return False
    end = m.start()

    block = rest[:end]
    for line in block.splitlines():
        line = line.strip()
        if re.match(r"^background\s*:", line, re.IGNORECASE):
            _, _, value = line.partition(":")
            return value.strip().lower() in _BACKGROUND_TRUE_VALUES

    return False


data = json.load(sys.stdin)
tool = data.get("tool_name", "")
inp = data.get("tool_input", {})

if tool not in AGENT_TOOL_NAMES:
    sys.exit(0)

# Signal 1: run_in_background in tool_input (available when schema includes the field).
if inp.get("run_in_background") is True:
    sys.exit(0)

# Signal 2: background: true in prompt frontmatter (schema-safe workaround for #1872).
# When the Agent schema strips run_in_background (additionalProperties: false),
# the dispatcher signals background intent via the prompt frontmatter instead.
prompt = inp.get("prompt", "")
if _has_background_true_in_frontmatter(prompt):
    sys.exit(0)

# Only enforce for the dispatcher. Subagents may call Agent synchronously.
if not is_dispatcher(data):
    sys.exit(0)

print(
    "BLOCKED: Dispatcher called Agent without background intent. "
    "The Agent tool schema may strip run_in_background before the hook sees it "
    "(issue #1872). Include `background: true` in the YAML frontmatter of the prompt:\n\n"
    "  ---\n"
    "  task_id: <slug>\n"
    "  chat_id: <id>\n"
    "  source: telegram\n"
    "  background: true\n"
    "  ---\n\n"
    "This ensures background intent is visible to the hook regardless of schema "
    "validation. The result will be delivered via write_result.",
    file=sys.stderr,
)
sys.exit(2)
