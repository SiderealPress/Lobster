"""Pure checks for the spawn-prompt frontmatter contract.

Used by require-task-id-in-prompt.py (PreToolUse on Agent). The hook keeps all
I/O (stdin, stderr, the warn log, exit codes); everything here is pure so it
can be tested without running the hook.

Contract: every spawned-agent prompt opens with a frontmatter block carrying
task_id, chat_id and source. chat_id: 0 means "no user to answer" and is only
valid when task_origin is scheduled or internal; a user-originated spawn must
carry the user's real chat_id, otherwise its result has nowhere to go.

    ---
    task_id: my-task
    chat_id: 1000001
    source: telegram
    task_origin: user        # optional: user | scheduled | internal
    ---
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, NamedTuple

MODE_ENV = "LOBSTER_SPAWN_FRONTMATTER_MODE"
WARN_LOG_ENV = "LOBSTER_SPAWN_FRONTMATTER_WARN_LOG"

MODE_WARN = "warn"
MODE_BLOCK = "block"
DEFAULT_MODE = MODE_WARN
VALID_MODES = (MODE_WARN, MODE_BLOCK)

WARN_LOG_FILENAME = "spawn-frontmatter-warn.log"

NON_USER_CHAT_ID = "0"
NON_USER_ORIGINS = ("scheduled", "internal")

# Problem codes, in the order they are reported.
MISSING_CHAT_ID = "chat_id"
MISSING_SOURCE = "source"
ZERO_CHAT_ID_WITHOUT_NON_USER_ORIGIN = "task_origin"

_FIX_HINTS = {
    MISSING_CHAT_ID: (
        "add `chat_id: <chat_id of the user this work is for>` "
        "(or `chat_id: 0` together with `task_origin: scheduled|internal` "
        "for work that is not for a user)"
    ),
    MISSING_SOURCE: "add `source: <channel the reply goes to, e.g. telegram, slack, system>`",
    ZERO_CHAT_ID_WITHOUT_NON_USER_ORIGIN: (
        "`chat_id: 0` is only valid for non-user work: add `task_origin: internal` "
        "or `task_origin: scheduled`, or replace it with the user's real chat_id"
    ),
}


class SpawnCheck(NamedTuple):
    """Outcome of checking one spawn prompt under one mode."""

    mode: str
    problems: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.problems) and self.mode == MODE_BLOCK

    @property
    def warn(self) -> bool:
        return bool(self.problems) and self.mode != MODE_BLOCK


def _unquote(value: str) -> str:
    return value.strip().strip("\"'").strip()


def parse_frontmatter(prompt: str) -> dict[str, str]:
    """Return the simple `key: value` pairs of a leading frontmatter block.

    Same constrained format the hooks already accept: the prompt (after
    leading whitespace) starts with `---` and a later line starts with `---`.
    Returns {} when there is no closed block. Values are unquoted and stripped.
    """
    text = prompt.lstrip()
    if not text.startswith("---"):
        return {}
    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}
    pairs = (
        line.strip().partition(":")
        for line in rest[:end].splitlines()
        if ":" in line and not line.strip().startswith("#")
    )
    return {key.strip(): _unquote(value) for key, _, value in pairs if key.strip()}


def find_problems(frontmatter: Mapping[str, str]) -> tuple[str, ...]:
    """Return the problem codes for a parsed frontmatter, in reporting order."""
    chat_id = frontmatter.get("chat_id", "")
    origin = frontmatter.get("task_origin", "").lower()
    checks = (
        (MISSING_CHAT_ID, not chat_id),
        (MISSING_SOURCE, not frontmatter.get("source", "")),
        (
            ZERO_CHAT_ID_WITHOUT_NON_USER_ORIGIN,
            chat_id == NON_USER_CHAT_ID and origin not in NON_USER_ORIGINS,
        ),
    )
    return tuple(code for code, failed in checks if failed)


def resolve_mode(raw: str | None) -> str:
    """Normalise the configured mode; anything unrecognised means warn."""
    value = (raw or "").strip().lower()
    return value if value in VALID_MODES else DEFAULT_MODE


def evaluate_spawn(prompt: str, mode: str) -> SpawnCheck:
    """Check a spawn prompt's frontmatter under the given (resolved) mode."""
    return SpawnCheck(mode=mode, problems=find_problems(parse_frontmatter(prompt)))


def resolve_warn_log_path(env: Mapping[str, str], home: Path) -> Path:
    """Warn log: explicit override, else $LOBSTER_WORKSPACE/logs, else ~/lobster-workspace/logs."""
    override = env.get(WARN_LOG_ENV, "").strip()
    if override:
        return Path(override)
    workspace = env.get("LOBSTER_WORKSPACE", "").strip()
    base = Path(workspace) if workspace else home / "lobster-workspace"
    return base / "logs" / WARN_LOG_FILENAME


def format_warn_line(timestamp: str, subagent_type: str | None, problems: tuple[str, ...]) -> str:
    """One log line: timestamp, subagent_type, what is missing."""
    return (
        f"{timestamp} subagent_type={subagent_type or '-'} "
        f"missing={','.join(problems)}"
    )


def format_fix_hints(problems: tuple[str, ...]) -> str:
    return " ".join(f"[{code}] {_FIX_HINTS[code]}." for code in problems)


def format_block_message(problems: tuple[str, ...]) -> str:
    return (
        "BLOCKED: Agent spawn prompt frontmatter is incomplete. "
        f"{format_fix_hints(problems)} "
        f"(Enforced because {MODE_ENV}={MODE_BLOCK}.)"
    )


def format_warn_message(problems: tuple[str, ...]) -> str:
    return (
        "Note: Agent spawn prompt frontmatter is incomplete (allowed; "
        f"{MODE_ENV}={MODE_WARN}). {format_fix_hints(problems)}"
    )
