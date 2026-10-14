"""
Agent definitions must not hardcode the task_id they pass to write_result.

The dispatcher spawns an agent with a ``task_id`` in the prompt frontmatter,
and the auto-register hook keys the agent_sessions row by that task_id.
``write_result`` auto-unregisters the session by matching the task_id it is
given (``WHERE id = ? OR task_id = ?``). If an agent definition tells the
agent to pass a fixed literal instead of the task_id it was spawned with, the
two diverge whenever the dispatcher uses a different (or per-invocation)
task_id, the UPDATE matches zero rows, and the session stays ``running``
forever even though the result was delivered (issue #2257).

Because the dispatcher may now append a per-invocation suffix to catch-up
task_ids, the bootup handlers must match catch-up results by task_id prefix
rather than by exact string.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
DISPATCHER_BOOTUP = REPO_ROOT / ".claude" / "sys.dispatcher.bootup.md"
COMPACT_DISPATCHER_BOOTUP = REPO_ROOT / ".claude" / "sys.compact-dispatcher.bootup.md"

STARTUP_CATCHUP_PREFIX = "startup-catchup"
COMPACT_CATCHUP_PREFIX = "compact-catchup"

# A multi-line write_result example call, captured up to the first ")" that
# starts a line (the closing paren of the call in agent-definition examples).
_WRITE_RESULT_CALL = re.compile(r"write_result\((?P<args>.*?)\n\s*\)", re.DOTALL)

# task_id bound to a concrete string literal. Placeholders such as
# "<task_id from your prompt>" and f-strings are not concrete identifiers and
# are therefore allowed.
_LITERAL_TASK_ID = re.compile(r"""\btask_id\s*=\s*(["'])(?P<value>[^"'<>{}\n]+)\1""")


def find_hardcoded_task_ids(text: str) -> list[str]:
    """Return every concrete string literal passed as task_id to write_result."""
    return [
        literal.group("value")
        for call in _WRITE_RESULT_CALL.finditer(text)
        for literal in _LITERAL_TASK_ID.finditer(call.group("args"))
    ]


def lines_mentioning_prefix_match(text: str, identifier: str) -> list[str]:
    """Lines that name ``identifier`` and describe matching it as a prefix."""
    return [
        line
        for line in text.splitlines()
        if identifier in line and "prefix" in line.lower()
    ]


# ---------------------------------------------------------------------------
# Scanner self-checks (so a regex regression cannot silently pass the sweep)
# ---------------------------------------------------------------------------


def test_scanner_flags_a_fixed_literal_task_id():
    sample = 'mcp__lobster-inbox__write_result(\n    task_id="catchup-fixture-a",\n    chat_id=0,\n)\n'
    assert find_hardcoded_task_ids(sample) == ["catchup-fixture-a"]


def test_scanner_allows_task_id_from_prompt():
    samples = [
        "write_result(\n    task_id=task_id,\n    chat_id=0,\n)\n",
        'write_result(\n    task_id="<task_id from your prompt frontmatter>",\n)\n',
        'write_result(\n    task_id=f"issue-{issue_number}",\n)\n',
    ]
    assert all(find_hardcoded_task_ids(s) == [] for s in samples)


# ---------------------------------------------------------------------------
# Repository sweeps
# ---------------------------------------------------------------------------


def test_no_agent_definition_hardcodes_write_result_task_id():
    offenders = {
        path.name: ids
        for path in sorted(AGENTS_DIR.glob("*.md"))
        if (ids := find_hardcoded_task_ids(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        "Agent definitions must pass the task_id from their prompt frontmatter "
        f"to write_result, never a fixed string. Offenders: {offenders}"
    )


@pytest.mark.parametrize(
    "bootup_file, identifier",
    [
        (DISPATCHER_BOOTUP, STARTUP_CATCHUP_PREFIX),
        (DISPATCHER_BOOTUP, COMPACT_CATCHUP_PREFIX),
        (COMPACT_DISPATCHER_BOOTUP, COMPACT_CATCHUP_PREFIX),
    ],
)
def test_bootup_handlers_match_catchup_prefix(bootup_file: Path, identifier: str):
    text = bootup_file.read_text(encoding="utf-8")
    assert lines_mentioning_prefix_match(text, identifier), (
        f"{bootup_file.name} must match {identifier!r} catch-up results by "
        "task_id prefix (a per-invocation suffix may be appended)"
    )
