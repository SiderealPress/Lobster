#!/usr/bin/env python3
"""
Shared utility: which Agent ``subagent_type`` values are built-in helpers.

Claude Code ships read-only helper agents (``Explore``, ``Plan``) that a
session may spawn for a quick search or a plan. They never run Lobster's
subagent protocol: they carry no task_id, never call ``write_result`` and
report back to their caller inline. Pushing them through the task_id /
agent_sessions / write_result bookkeeping costs a fake task_id, a DB row, an
inflight-work line, up to MAX_HOOK_FIRES blocked exits and, when the helper
never calls write_result, a spurious completion or ghost notice.

The hooks that implement that bookkeeping consult this module to skip it:

- ``require-task-id-in-prompt.py`` (PreToolUse): allow the spawn without a task_id
- ``auto-register-agent.py`` (PostToolUse): write no DB row and no inflight entry
- ``require-write-result.py`` (SubagentStop): allow exit without write_result

The allowlist is exact-name and comes from ``LOBSTER_HELPER_AGENT_TYPES``
(comma-separated). Unset means the default ``Explore,Plan``; an empty value
disables the exemption entirely.
"""

import json
import os
from pathlib import Path
from typing import Mapping

HELPER_AGENT_TYPES_ENV = "LOBSTER_HELPER_AGENT_TYPES"
DEFAULT_HELPER_AGENT_TYPES: frozenset[str] = frozenset({"Explore", "Plan"})


def parse_helper_agent_types(raw: str | None) -> frozenset[str]:
    """Parse the env value into an exact-name allowlist.

    ``None`` (unset) yields the default; ``""`` yields an empty set, which
    disables the exemption. Whitespace and empty items are ignored.
    """
    if raw is None:
        return DEFAULT_HELPER_AGENT_TYPES
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def helper_agent_types(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Return the active allowlist, read from ``environ`` (default ``os.environ``)."""
    env = os.environ if environ is None else environ
    return parse_helper_agent_types(env.get(HELPER_AGENT_TYPES_ENV))


def is_helper_agent_type(
    agent_type: object, environ: Mapping[str, str] | None = None
) -> bool:
    """True when ``agent_type`` is a non-empty string on the active allowlist."""
    return (
        isinstance(agent_type, str)
        and bool(agent_type)
        and agent_type in helper_agent_types(environ)
    )


def _meta_json_path(agent_transcript_path: str) -> Path | None:
    """Sibling ``agent-<id>.meta.json`` of an ``agent-<id>.jsonl`` transcript."""
    if not agent_transcript_path.endswith(".jsonl"):
        return None
    return Path(agent_transcript_path[: -len(".jsonl")] + ".meta.json")


def subagent_stop_agent_type(data: Mapping[str, object]) -> str | None:
    """Return the agent type named by a SubagentStop hook payload.

    Claude Code puts the spawn's ``subagent_type`` in the payload's
    ``agent_type`` field. Payloads without it fall back to the ``agentType``
    field of the ``.meta.json`` file Claude Code writes next to the agent
    transcript at spawn time. Returns None when neither source names a type.
    Never raises.
    """
    agent_type = data.get("agent_type")
    if isinstance(agent_type, str) and agent_type:
        return agent_type
    transcript_path = data.get("agent_transcript_path")
    if not isinstance(transcript_path, str):
        return None
    meta_path = _meta_json_path(transcript_path)
    if meta_path is None:
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    meta_type = meta.get("agentType") if isinstance(meta, dict) else None
    return meta_type if isinstance(meta_type, str) and meta_type else None
