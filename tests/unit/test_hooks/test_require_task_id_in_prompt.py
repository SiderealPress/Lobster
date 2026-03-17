"""
Unit tests for hooks/require-task-id-in-prompt.py

Tests cover:
- Non-Agent tool calls pass through (exit 0)
- YAML frontmatter with task_id passes (exit 0)
- YAML frontmatter without task_id is blocked (exit 2)
- Legacy "task_id is: X" text passes (exit 0)
- Prompt with both frontmatter and legacy text passes (exit 0)
- Prompt with neither format is blocked (exit 2)
- Empty frontmatter task_id value is blocked (exit 2)
- Block message appears on stderr
- Malformed stdin is tolerated (exit 0)
"""

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = _HOOKS_DIR / "require-task-id-in-prompt.py"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_hook(hook_input: dict) -> tuple[int, str, str]:
    stdout_cap = StringIO()
    stderr_cap = StringIO()
    stdin_data = json.dumps(hook_input)
    exit_code = None
    with (
        patch("sys.stdin", StringIO(stdin_data)),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
    ):
        try:
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code
    return exit_code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _make_input(tool_name: str = "Agent", prompt: str = "") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-test",
        "tool_name": tool_name,
        "tool_input": {"prompt": prompt},
    }


# ---------------------------------------------------------------------------
# Non-Agent tools
# ---------------------------------------------------------------------------

class TestNonAgentPassthrough:
    def test_bash_exits_0(self):
        exit_code, _, _ = _run_hook(_make_input("Bash", "no id here"))
        assert exit_code == 0

    def test_read_exits_0(self):
        exit_code, _, _ = _run_hook(_make_input("Read", ""))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# YAML frontmatter format
# ---------------------------------------------------------------------------

class TestYamlFrontmatter:
    def test_frontmatter_with_task_id_passes(self):
        prompt = "---\ntask_id: my-task\nchat_id: 123\n---\nDo work."
        exit_code, _, _ = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 0

    def test_frontmatter_without_task_id_blocked(self):
        prompt = "---\nchat_id: 123\nsource: telegram\n---\nDo work."
        exit_code, _, stderr = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 2
        assert "BLOCKED" in stderr

    def test_frontmatter_empty_task_id_blocked(self):
        """A task_id key with no value must not pass."""
        prompt = "---\ntask_id:\n---\nDo work."
        exit_code, _, stderr = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 2

    def test_frontmatter_unclosed_falls_back(self):
        """Unclosed frontmatter is not treated as valid frontmatter."""
        prompt = "---\ntask_id: no-close\n\nsome text"
        exit_code, _, stderr = _run_hook(_make_input(prompt=prompt))
        # No task_id found in either format -> blocked
        assert exit_code == 2

    def test_frontmatter_task_id_with_whitespace_passes(self):
        """Leading whitespace before --- is tolerated."""
        prompt = "\n---\ntask_id: ws-task\n---\n"
        exit_code, _, _ = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Legacy text format
# ---------------------------------------------------------------------------

class TestLegacyTextFormat:
    def test_legacy_format_passes(self):
        prompt = "Your task_id is: my-slug\nDo the work."
        exit_code, _, _ = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 0

    def test_legacy_format_case_insensitive(self):
        prompt = "TASK_ID IS: uppercase-slug"
        exit_code, _, _ = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 0

    def test_legacy_format_inline(self):
        prompt = "Please run. task_id is: inline-id. Thanks."
        exit_code, _, _ = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Neither format
# ---------------------------------------------------------------------------

class TestNoTaskId:
    def test_plain_prompt_blocked(self):
        prompt = "Just do some stuff."
        exit_code, _, stderr = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 2
        assert "BLOCKED" in stderr

    def test_empty_prompt_blocked(self):
        exit_code, _, _ = _run_hook(_make_input(prompt=""))
        assert exit_code == 2

    def test_block_message_on_stderr_not_stdout(self):
        exit_code, stdout, stderr = _run_hook(_make_input(prompt="no id"))
        assert exit_code == 2
        assert "BLOCKED" in stderr
        assert "BLOCKED" not in stdout


# ---------------------------------------------------------------------------
# Both formats present
# ---------------------------------------------------------------------------

class TestBothFormats:
    def test_both_present_passes(self):
        prompt = "---\ntask_id: yaml-id\n---\nYour task_id is: legacy-id"
        exit_code, _, _ = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Error tolerance
# ---------------------------------------------------------------------------

class TestErrorTolerance:
    def test_malformed_json_exits_0(self):
        stdout_cap = StringIO()
        stderr_cap = StringIO()
        exit_code = None
        with (
            patch("sys.stdin", StringIO("{not valid")),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
        ):
            try:
                hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
                exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
            except SystemExit as e:
                exit_code = e.code
        assert exit_code == 0
