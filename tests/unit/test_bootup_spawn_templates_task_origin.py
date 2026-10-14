"""
Every ``chat_id: 0`` spawn template in the bootup files must declare a
non-user ``task_origin``.

The spawn-prompt frontmatter contract (``.claude/sys.dispatcher.bootup.md``,
"Spawn-prompt frontmatter contract") says ``chat_id: 0`` is only valid for
work that is not for a user, marked ``task_origin: scheduled`` (scheduled
jobs) or ``task_origin: internal`` (catch-up, consolidation, session notes,
other system work). ``hooks/require-task-id-in-prompt.py`` checks exactly
that: in warn mode a ``chat_id: 0`` spawn without such a ``task_origin``
writes a warn-log line, and with ``LOBSTER_SPAWN_FRONTMATTER_MODE=block`` it
is rejected.

The dispatcher's own system spawns (startup/compact catch-up, session-note
polish/append/update, scheduled-job dispatch, nightly consolidation) are
written from the templates in the ``.claude/sys.*.bootup.md`` files. If a
template there omits ``task_origin``, every install logs a warning for each
of those spawns, and block mode would reject the dispatcher's own bookkeeping
spawns. This test keeps the templates in line with the contract.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTUP_FILES = sorted((REPO_ROOT / ".claude").glob("sys.*.bootup.md"))

_CHAT_ID_ZERO = re.compile(r"\bchat_id:\s*0\b")
_TASK_ID_KEY = re.compile(r"\btask_id:")
_NON_USER_ORIGIN = re.compile(r"\btask_origin:\s*\"?(internal|scheduled)\"?\b")
# Result-handling prose ("the result arrives as a subagent_result with
# task_id ... and chat_id: 0") names the same keys without spawning anything.
_RESULT_PROSE = re.compile(r"subagent_result|result arrives", re.IGNORECASE)

# How far a frontmatter template may spread its keys across lines (multi-line
# f-strings and fenced ``---`` blocks put one key per line).
_WINDOW = 4


def _unescape(text: str) -> str:
    """Turn literal ``\\n`` sequences (f-string prompt templates) into newlines."""
    return text.replace("\\n", "\n")


def _spawn_template_lines(lines: list[str]) -> list[int]:
    """Indexes of lines that are part of a spawn template using ``chat_id: 0``.

    A spawn template is a ``chat_id: 0`` line that also names a ``task_id:``
    key, either on the same line or within the preceding ``_WINDOW`` lines.
    Prose that merely mentions ``chat_id: 0`` (result-handling rules, the
    contract paragraph itself, "never send_reply") has no ``task_id:`` key
    nearby and is not a template.
    """
    hits = []
    for i, line in enumerate(lines):
        if not _CHAT_ID_ZERO.search(_unescape(line)):
            continue
        if _RESULT_PROSE.search(_unescape("\n".join(lines[max(0, i - 1): i + 1]))):
            continue  # this line or the one before describes a result arriving
        context = _unescape("\n".join(lines[max(0, i - _WINDOW): i + 1]))
        if _TASK_ID_KEY.search(context):
            hits.append(i)
    return hits


def _declares_non_user_origin(lines: list[str], i: int) -> bool:
    """A one-line template must carry task_origin on that line; a multi-line
    template (one key per line) may carry it within ``_WINDOW`` lines."""
    line = _unescape(lines[i])
    if _TASK_ID_KEY.search(line):
        return bool(_NON_USER_ORIGIN.search(line))
    window = _unescape("\n".join(lines[max(0, i - _WINDOW): i + _WINDOW + 1]))
    return bool(_NON_USER_ORIGIN.search(window))


def test_bootup_files_are_present():
    names = {p.name for p in BOOTUP_FILES}
    assert "sys.dispatcher.bootup.md" in names
    assert "sys.compact-dispatcher.bootup.md" in names


@pytest.mark.parametrize("bootup", BOOTUP_FILES, ids=lambda p: p.name)
def test_chat_id_zero_spawn_templates_declare_task_origin(bootup: Path):
    lines = bootup.read_text().splitlines()
    missing = [
        f"{bootup.name}:{i + 1}: {lines[i].strip()[:100]}"
        for i in _spawn_template_lines(lines)
        if not _declares_non_user_origin(lines, i)
    ]
    assert not missing, (
        "chat_id: 0 spawn template(s) without task_origin: internal|scheduled "
        "(the spawn check in hooks/require-task-id-in-prompt.py warns or blocks "
        "these):\n" + "\n".join(missing)
    )


def test_detector_finds_templates_and_ignores_prose():
    lines = [
        "**When the result arrives** (`chat_id: 0`): relay nothing.",
        "",
        "   - Pass: task_id: \"session-note-polish\", chat_id: 0, source: \"system\"",
        "   f\"task_id: {tid}\\n\"",
        "   f\"chat_id: 0\\n\"",
        "   f\"source: system\\n\"",
        "   f\"task_origin: internal\\n\"",
        "",
        "",
        "",
        "",
        "   prompt = f\"---\\ntask_id: job-{name}\\nchat_id: 0\\nsource: system\\n---\\n\\n{body}\"",
        "   prompt = f\"---\\ntask_id: job-{name}\\nchat_id: 0\\nsource: system\\ntask_origin: scheduled\\n---\\n\"",
    ]
    hits = _spawn_template_lines(lines)
    assert hits == [2, 4, 11, 12]
    assert not _declares_non_user_origin(lines, 2)
    assert _declares_non_user_origin(lines, 4)
    # One-line templates are judged on their own line, so an adjacent template
    # that does declare task_origin cannot vouch for one that does not.
    assert not _declares_non_user_origin(lines, 11)
    assert _declares_non_user_origin(lines, 12)
    prose = [
        "The branch-check result arrives as a `subagent_result` with",
        "`task_id: \"debug-branch-check\"` and `chat_id: 0`. Process it silently.",
    ]
    assert _spawn_template_lines(prose) == []
