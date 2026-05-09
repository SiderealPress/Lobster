"""
Unit tests for hooks/require-subagent-type.py

Tests cover:
- Non-Agent tool calls pass through (exit 0)
- Agent with a valid subagent_type passes through (exit 0)
- Agent with subagent_type='review' passes through (exit 0)
- Agent without subagent_type is hard-blocked (exit 2)
- Agent with subagent_type='general-purpose' is hard-blocked (exit 2)
- Block messages go to stderr, not stdout
- Error messages enumerate all known agent types including 'review'
- KNOWN_AGENT_TYPES spot-checks key types and aligns with .claude/agents/ files
"""

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "require-subagent-type.py"
AGENTS_DIR = Path(__file__).parents[3] / ".claude" / "agents"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hook(hook_input: dict) -> tuple[int, str, str]:
    """Run the hook script and return (exit_code, stdout, stderr)."""
    stdout_capture = StringIO()
    stderr_capture = StringIO()
    stdin_data = json.dumps(hook_input)

    exit_code = None
    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_capture),
        patch("sys.stderr", stderr_capture),
    ):
        try:
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()


def _make_hook_input(tool_name: str, tool_input: dict) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-001",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


def _load_known_agent_types() -> tuple:
    """Load KNOWN_AGENT_TYPES from the hook module at import time."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_require_subagent_type_loader", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    fake_stdin = json.dumps({"tool_name": "Bash", "tool_input": {}})
    with (
        patch("sys.stdin", StringIO(fake_stdin)),
        patch("sys.stdout", StringIO()),
        patch("sys.stderr", StringIO()),
    ):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
    return mod.KNOWN_AGENT_TYPES


# Single source of truth: import from the hook rather than duplicating the list.
KNOWN_AGENT_TYPES = _load_known_agent_types()


# ---------------------------------------------------------------------------
# Non-Agent tool passthrough
# ---------------------------------------------------------------------------


class TestNonAgentTool:
    def test_bash_tool_exits_0(self):
        """Non-Agent tools must pass through without modification."""
        hook_input = _make_hook_input("Bash", {"command": "ls"})
        exit_code, stdout, stderr = _run_hook(hook_input)
        # The hook calls sys.exit(0) explicitly for non-Agent tools
        assert exit_code == 0

    def test_mcp_tool_exits_0(self):
        """MCP tools are not the Agent tool and must pass through."""
        hook_input = _make_hook_input(
            "mcp__lobster-inbox__check_inbox", {"limit": 10}
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0

    def test_read_tool_exits_0(self):
        """Read tool is not Agent and must pass through."""
        hook_input = _make_hook_input("Read", {"file_path": "/tmp/foo"})
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Valid subagent_type values pass through
# ---------------------------------------------------------------------------


class TestValidSubagentTypes:
    @pytest.mark.parametrize("agent_type", KNOWN_AGENT_TYPES)
    def test_known_agent_type_exits_0(self, agent_type):
        """Every known agent type must be allowed through (exit 0 or natural return)."""
        hook_input = _make_hook_input(
            "Agent",
            {"subagent_type": agent_type, "prompt": "do work"},
        )
        exit_code, _, stderr = _run_hook(hook_input)
        # None means the hook returned normally without calling sys.exit — equivalent to exit 0
        assert exit_code in (0, None), (
            f"Known agent type '{agent_type}' should be allowed, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_review_agent_type_passes_through(self):
        """subagent_type='review' must pass through — dispatcher uses it for engineer→reviewer routing."""
        hook_input = _make_hook_input(
            "Agent",
            {"subagent_type": "review", "prompt": "Review PR https://github.com/org/repo/pull/42"},
        )
        exit_code, _, stderr = _run_hook(hook_input)
        # None means the hook returned normally without calling sys.exit — equivalent to exit 0
        assert exit_code in (0, None), (
            f"subagent_type='review' should be allowed (used by engineer→reviewer routing). "
            f"Got exit {exit_code}. stderr={stderr!r}"
        )

    def test_unknown_custom_type_passes_through(self):
        """An unknown but non-empty type must pass through — the hook only blocks missing and 'general-purpose'."""
        hook_input = _make_hook_input(
            "Agent",
            {"subagent_type": "some-custom-agent", "prompt": "do work"},
        )
        exit_code, _, _ = _run_hook(hook_input)
        # None means the hook returned normally without calling sys.exit — equivalent to exit 0
        assert exit_code in (0, None)


# ---------------------------------------------------------------------------
# Missing subagent_type is hard-blocked
# ---------------------------------------------------------------------------


class TestMissingSubagentType:
    def test_agent_without_subagent_type_exits_2(self):
        """Agent called with no subagent_type is hard-blocked (exit 2)."""
        hook_input = _make_hook_input("Agent", {"prompt": "do work"})
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 2

    def test_block_message_goes_to_stderr(self):
        """Block message for missing subagent_type must go to stderr."""
        hook_input = _make_hook_input("Agent", {"prompt": "do work"})
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2
        assert "BLOCKED" in stderr
        assert "BLOCKED" not in stdout

    def test_block_message_mentions_review(self):
        """Missing subagent_type error message must list 'review' as a valid option."""
        hook_input = _make_hook_input("Agent", {"prompt": "do work"})
        _, _, stderr = _run_hook(hook_input)
        assert "review" in stderr, (
            f"Error message should mention 'review' as a valid agent type. stderr={stderr!r}"
        )

    def test_block_message_mentions_lobster_generalist(self):
        """Missing subagent_type error message must recommend lobster-generalist."""
        hook_input = _make_hook_input("Agent", {"prompt": "do work"})
        _, _, stderr = _run_hook(hook_input)
        assert "lobster-generalist" in stderr

    def test_agent_with_empty_subagent_type_exits_2(self):
        """Agent with empty string subagent_type is treated as missing (exit 2)."""
        hook_input = _make_hook_input("Agent", {"subagent_type": "", "prompt": "do work"})
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 2

    def test_agent_with_none_subagent_type_exits_2(self):
        """Agent with null subagent_type is treated as missing (exit 2)."""
        hook_input = _make_hook_input("Agent", {"subagent_type": None, "prompt": "do work"})
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 2


# ---------------------------------------------------------------------------
# general-purpose subagent_type is hard-blocked
# ---------------------------------------------------------------------------


class TestGeneralPurposeBlocked:
    def test_general_purpose_exits_2(self):
        """subagent_type='general-purpose' is not used in Lobster — must be hard-blocked."""
        hook_input = _make_hook_input(
            "Agent",
            {"subagent_type": "general-purpose", "prompt": "do work"},
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 2

    def test_general_purpose_block_message_goes_to_stderr(self):
        """general-purpose block message must go to stderr, not stdout."""
        hook_input = _make_hook_input(
            "Agent",
            {"subagent_type": "general-purpose", "prompt": "do work"},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2
        assert "BLOCKED" in stderr
        assert "BLOCKED" not in stdout

    def test_general_purpose_block_message_recommends_lobster_generalist(self):
        """general-purpose block message must recommend lobster-generalist."""
        hook_input = _make_hook_input(
            "Agent",
            {"subagent_type": "general-purpose", "prompt": "do work"},
        )
        _, _, stderr = _run_hook(hook_input)
        assert "lobster-generalist" in stderr

    def test_general_purpose_block_message_mentions_review(self):
        """general-purpose block message must list 'review' as an alternative."""
        hook_input = _make_hook_input(
            "Agent",
            {"subagent_type": "general-purpose", "prompt": "do work"},
        )
        _, _, stderr = _run_hook(hook_input)
        assert "review" in stderr, (
            f"general-purpose error message should list 'review' as an option. stderr={stderr!r}"
        )


# ---------------------------------------------------------------------------
# KNOWN_AGENT_TYPES constant alignment check
# ---------------------------------------------------------------------------


class TestKnownAgentTypesConstant:
    def test_review_is_in_known_types(self):
        """KNOWN_AGENT_TYPES must include 'review' since dispatcher uses subagent_type='review'."""
        assert "review" in KNOWN_AGENT_TYPES, (
            "KNOWN_AGENT_TYPES must include 'review' — dispatcher uses subagent_type='review' "
            "for engineer→reviewer routing."
        )

    def test_functional_engineer_is_in_known_types(self):
        """KNOWN_AGENT_TYPES must include 'functional-engineer'."""
        assert "functional-engineer" in KNOWN_AGENT_TYPES

    def test_known_types_matches_agents_directory(self):
        """KNOWN_AGENT_TYPES must contain exactly the agent types defined in .claude/agents/.

        Each .md file in .claude/agents/ defines one agent type (filename without extension).
        This test catches a new agent file being added without updating KNOWN_AGENT_TYPES,
        or a stale entry left after an agent file is removed.
        """
        files_on_disk = {p.stem for p in AGENTS_DIR.glob("*.md")}
        constant_set = set(KNOWN_AGENT_TYPES)
        missing_from_constant = files_on_disk - constant_set
        extra_in_constant = constant_set - files_on_disk
        assert not missing_from_constant and not extra_in_constant, (
            f"KNOWN_AGENT_TYPES is out of sync with .claude/agents/. "
            f"Missing from constant: {sorted(missing_from_constant)}. "
            f"Extra in constant (no matching file): {sorted(extra_in_constant)}."
        )
