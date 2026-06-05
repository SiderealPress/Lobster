"""
Unit tests for hooks/require-background-agent.py

Tests cover:
- Non-Agent tool calls pass through (exit 0)
- Agent with run_in_background=True passes through (exit 0)
- Agent without run_in_background called by dispatcher: hard block (exit 2)
- Agent without run_in_background called by subagent: allowed (exit 0)
- Dispatcher detection via startup flag file
- Missing run_in_background key (falsy) from dispatcher: blocked (exit 2)
- Block message goes to stderr
- Frontmatter sentinel: background: true in YAML frontmatter allows call (exit 0)
  even when run_in_background is stripped by schema validation
- Frontmatter sentinel: absent or false means hard block for dispatcher (exit 2)
- Sentinel is case-insensitive (background: True, background: true both accepted)
- Subagent with no sentinel is still allowed (no enforcement for subagents)
- _has_background_true_in_frontmatter: pure-function edge cases (leading whitespace,
  old-style run_in_background key, background: yes/1 variants, unclosed frontmatter)
"""

import importlib.util
import json
import os
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

# Add hooks directory to path so session_role can be imported by both
# the hook under test (via exec) and directly by test methods.
_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOKS_DIR = _HOOKS_DIR
HOOK_PATH = HOOKS_DIR / "require-background-agent.py"


def _load_hook(monkeypatch, tmp_path):
    """Load require-background-agent.py as a fresh module for each test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("require_background_agent", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hook(hook_input: dict) -> tuple[int, str, str]:
    """
    Run the hook script as a subprocess-like call via exec.
    Returns (exit_code, stdout_text, stderr_text).
    """
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
            # Execute the hook script directly; __file__ must be set so the
            # hook's sys.path.insert(0, Path(__file__).parent) works correctly.
            hook_globals = {"__name__": "__main__", "__file__": str(HOOK_PATH)}
            exec(compile(HOOK_PATH.read_text(), str(HOOK_PATH), "exec"), hook_globals)
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()


def _make_hook_input(
    tool_name: str,
    tool_input: dict,
    session_id: str = "sess-sub-001",
) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


def _setup_dispatcher_marker(tmp_path: Path, session_id: str) -> None:
    """Write the dispatcher session marker file under tmp_path/messages/config/.

    Kept for tests that still set up the legacy DISPATCHER_SESSION_FILE.
    This file is no longer used by is_dispatcher() — use _setup_startup_flag()
    and _patch_startup_flag() to simulate the dispatcher via the startup flag.
    """
    config_dir = tmp_path / "messages" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "dispatcher-session-id").write_text(session_id)


def _setup_startup_flag(tmp_path: Path) -> Path:
    """Write the dispatcher startup flag file with the current process PID.

    is_dispatcher() reads this file and checks that the PID is alive via
    kill(pid, 0). Writing os.getpid() makes the check succeed in the test
    process, simulating an active dispatcher launcher.

    Returns the Path of the flag file.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    flag_file = data_dir / "dispatcher-startup-flag"
    flag_file.write_text(str(os.getpid()))
    return flag_file


def _patch_startup_flag(monkeypatch, tmp_path: Path) -> Path:
    """Patch session_role.STARTUP_FLAG_FILE to a temp file with the live PID.

    This is the correct way to simulate the dispatcher in tests under the
    simplified detection scheme (issue #1908). The startup flag replaces the
    legacy DISPATCHER_SESSION_FILE as the primary dispatcher signal.

    Returns the Path of the patched flag file.
    """
    import session_role
    flag_file = _setup_startup_flag(tmp_path)
    monkeypatch.setattr(session_role, "STARTUP_FLAG_FILE", flag_file)
    return flag_file


# ---------------------------------------------------------------------------
# Non-Agent tool passthrough
# ---------------------------------------------------------------------------


class TestNonAgentTool:
    def test_non_agent_tool_exits_0(self, monkeypatch, tmp_path):
        """Any tool that is not Agent passes through immediately."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input("Bash", {"command": "ls"})
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0

    def test_mcp_tool_exits_0(self, monkeypatch, tmp_path):
        """MCP tools are not the Agent tool and must pass through."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "mcp__lobster-inbox__check_inbox", {}, session_id="dispatcher-sess"
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Agent with run_in_background=True
# ---------------------------------------------------------------------------


class TestAgentWithBackground:
    def test_agent_with_background_true_exits_0_dispatcher(self, monkeypatch, tmp_path):
        """Dispatcher calling Agent with run_in_background=True is always OK."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work", "run_in_background": True},
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0

    def test_agent_with_background_true_exits_0_subagent(self, monkeypatch, tmp_path):
        """Subagent calling Agent with run_in_background=True is also fine."""
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work", "run_in_background": True},
            session_id="subagent-sess-999",
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Dispatcher calling Agent synchronously (the bad case)
# ---------------------------------------------------------------------------


class TestDispatcherSynchronousAgent:
    def test_dispatcher_agent_no_background_key_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher omitting run_in_background is hard-blocked (exit 2)."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work"},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, f"Expected hard block (exit 2), got {exit_code}"

    def test_dispatcher_agent_background_false_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher passing run_in_background=False explicitly is hard-blocked."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work", "run_in_background": False},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, f"Expected hard block (exit 2), got {exit_code}"

    def test_block_message_goes_to_stderr(self, monkeypatch, tmp_path):
        """Block message must appear on stderr so Claude Code injects it as feedback."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work"},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2
        assert "BLOCKED" in stderr, f"Expected BLOCKED in stderr, got: {stderr!r}"
        assert "run_in_background" in stderr, f"Expected guidance in stderr, got: {stderr!r}"

    def test_block_message_not_in_stdout(self, monkeypatch, tmp_path):
        """Block message must not appear on stdout (stdout is for JSON responses)."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work"},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2
        assert "BLOCKED" not in stdout


# ---------------------------------------------------------------------------
# Subagent calling Agent synchronously (must be allowed)
# ---------------------------------------------------------------------------


class TestSubagentSynchronousAgent:
    def test_subagent_agent_no_background_exits_0(self, monkeypatch, tmp_path):
        """Subagents may call Agent synchronously — hook must not fire for them."""
        # Marker file points to a different session ID (dispatcher is someone else).
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do nested work"},
            session_id="subagent-sess-999",  # Different from dispatcher session
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Subagent should be allowed to call Agent synchronously, got exit {exit_code}. "
            f"stderr={stderr!r}"
        )

    def test_subagent_no_marker_file_exits_0(self, monkeypatch, tmp_path):
        """No marker file means is_dispatcher() returns False → treat as subagent → allow."""
        import session_role
        # Point to a nonexistent file so marker check returns None → fallback → False
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do nested work"},
            session_id="some-sess",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Missing marker file should default to subagent (allow), got exit {exit_code}. "
            f"stderr={stderr!r}"
        )


# ---------------------------------------------------------------------------
# "Task" tool name (older CC versions use Task instead of Agent)
# ---------------------------------------------------------------------------


class TestTaskToolName:
    """CC older versions use "Task" as the tool name for spawning subagents.

    The hook must treat "Task" identically to "Agent".
    """

    def test_task_tool_dispatcher_sync_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher calling Task (old CC) without run_in_background is hard-blocked."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Task",
            {"prompt": "do work"},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, (
            f"Dispatcher calling Task without run_in_background should be hard-blocked, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_task_tool_dispatcher_background_true_exits_0(self, monkeypatch, tmp_path):
        """Dispatcher calling Task with run_in_background=True is allowed."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Task",
            {"prompt": "do work", "run_in_background": True},
            session_id="dispatcher-sess-001",
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0

    def test_task_tool_subagent_sync_exits_0(self, monkeypatch, tmp_path):
        """Subagent calling Task synchronously is allowed (not the dispatcher)."""
        _setup_dispatcher_marker(tmp_path, "dispatcher-sess-001")
        import session_role
        monkeypatch.setattr(
            session_role, "DISPATCHER_SESSION_FILE",
            tmp_path / "messages" / "config" / "dispatcher-session-id",
        )
        hook_input = _make_hook_input(
            "Task",
            {"prompt": "do nested work"},
            session_id="subagent-sess-999",
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Subagent should be allowed to call Task synchronously, got exit {exit_code}. "
            f"stderr={stderr!r}"
        )


# ---------------------------------------------------------------------------
# Frontmatter sentinel: background: true in prompt YAML frontmatter
# ---------------------------------------------------------------------------
#
# When the Agent tool schema strips run_in_background (additionalProperties: false),
# the dispatcher can still signal background intent by including `background: true`
# in the prompt's YAML frontmatter block. This is the canonical workaround until
# the schema is fixed upstream.
#
# Related issue: #1872
# ---------------------------------------------------------------------------

FRONTMATTER_PROMPT_BACKGROUND_TRUE = """\
---
task_id: test-task
chat_id: 12345
source: telegram
background: true
---

Do some background work."""

FRONTMATTER_PROMPT_BACKGROUND_FALSE = """\
---
task_id: test-task
chat_id: 12345
source: telegram
background: false
---

Do some foreground work."""

FRONTMATTER_PROMPT_NO_BACKGROUND = """\
---
task_id: test-task
chat_id: 12345
source: telegram
---

Do some work without background key."""

FRONTMATTER_PROMPT_BACKGROUND_TRUE_UPPERCASE = """\
---
task_id: test-task
chat_id: 12345
source: telegram
background: True
---

Do some background work (Python-style True)."""


class TestFrontmatterSentinel:
    """Tests for the background: true YAML frontmatter sentinel.

    These tests verify the workaround for the Agent schema stripping run_in_background
    (issue #1872). The dispatcher includes `background: true` in the prompt frontmatter;
    the hook checks this as a secondary signal when run_in_background is absent.
    """

    def test_dispatcher_frontmatter_background_true_exits_0(self, monkeypatch, tmp_path):
        """Dispatcher prompt with background: true in frontmatter → allowed (exit 0).

        This is the primary fix for #1872: when the schema strips run_in_background,
        the dispatcher signals background intent via the prompt frontmatter instead.
        """
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": FRONTMATTER_PROMPT_BACKGROUND_TRUE},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Dispatcher with background: true in frontmatter should be allowed, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_dispatcher_frontmatter_background_false_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher prompt with background: false in frontmatter → hard block (exit 2).

        background: false explicitly opts out of background mode — must be blocked.
        """
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": FRONTMATTER_PROMPT_BACKGROUND_FALSE},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, (
            f"Dispatcher with background: false should be hard-blocked, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_dispatcher_frontmatter_no_background_key_exits_2(self, monkeypatch, tmp_path):
        """Dispatcher prompt with no background key in frontmatter → hard block (exit 2).

        Missing background key means no background intent declared — must be blocked.
        """
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": FRONTMATTER_PROMPT_NO_BACKGROUND},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, (
            f"Dispatcher without background key should be hard-blocked, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_dispatcher_frontmatter_background_uppercase_True_exits_0(
        self, monkeypatch, tmp_path
    ):
        """background: True (Python-style) in frontmatter → allowed (exit 0).

        Claude often writes True/False (Python style) in YAML-like blocks.
        The hook must accept both `true` and `True`.
        """
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": FRONTMATTER_PROMPT_BACKGROUND_TRUE_UPPERCASE},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"background: True (Python-style) should be accepted, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_subagent_no_background_sentinel_exits_0(self, monkeypatch, tmp_path):
        """Subagent without background sentinel is still allowed — enforcement is dispatcher-only."""
        # The startup flag is not set — is_dispatcher() returns False → subagent path → exit 0.
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": FRONTMATTER_PROMPT_NO_BACKGROUND},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Subagent without background sentinel should be allowed, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_block_message_mentions_frontmatter_sentinel(self, monkeypatch, tmp_path):
        """Block message must mention the frontmatter sentinel as the fix."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "do work"},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2
        assert "background: true" in stderr.lower(), (
            f"Block message should mention 'background: true' sentinel. stderr={stderr!r}"
        )

    def test_both_signals_present_exits_0(self, monkeypatch, tmp_path):
        """Both run_in_background=True in tool_input AND sentinel in frontmatter → allowed."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {
                "prompt": FRONTMATTER_PROMPT_BACKGROUND_TRUE,
                "run_in_background": True,
            },
        )
        exit_code, _, _ = _run_hook(hook_input)
        assert exit_code == 0


    def test_dispatcher_old_run_in_background_key_in_frontmatter_exits_2(
        self, monkeypatch, tmp_path
    ):
        """run_in_background: true in frontmatter is NOT the sentinel — must be blocked.

        The sentinel key is `background`, not `run_in_background`. A prompt that puts
        run_in_background in the frontmatter instead of background is missing the signal
        and must be blocked (the hook sees it only if CC passes it through tool_input,
        which it does not for Agent calls under additionalProperties: false).
        """
        _patch_startup_flag(monkeypatch, tmp_path)
        old_key_prompt = """\
---
task_id: test-task
chat_id: 12345
source: telegram
run_in_background: true
---

Do work."""
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": old_key_prompt},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, (
            f"run_in_background in frontmatter (not background:) should be blocked. "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_dispatcher_prompt_no_frontmatter_exits_2(self, monkeypatch, tmp_path):
        """Plain prompt (no frontmatter) from dispatcher → hard block (exit 2)."""
        _patch_startup_flag(monkeypatch, tmp_path)
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": "Just a plain prompt with no frontmatter at all."},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, (
            f"Plain prompt without frontmatter should be hard-blocked, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_dispatcher_frontmatter_leading_whitespace_exits_0(
        self, monkeypatch, tmp_path
    ):
        """Prompt with leading whitespace before --- is handled by lstrip() → allowed.

        The _has_background_true_in_frontmatter function strips leading whitespace
        before checking for the --- delimiter. This test verifies that a prompt that
        starts with a newline (e.g. from multi-line string formatting) is not rejected.
        """
        _patch_startup_flag(monkeypatch, tmp_path)
        prompt_with_leading_newline = "\n---\ntask_id: x\nbackground: true\n---\n\nWork."
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": prompt_with_leading_newline},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 0, (
            f"Prompt with leading whitespace before --- should be allowed, "
            f"got exit {exit_code}. stderr={stderr!r}"
        )

    def test_dispatcher_frontmatter_unclosed_exits_2(self, monkeypatch, tmp_path):
        """Frontmatter block with no closing --- is not recognised → hard block (exit 2).

        The function requires a closing --- delimiter. Without it, the block is not
        treated as valid frontmatter and the background signal is not detected.
        """
        _patch_startup_flag(monkeypatch, tmp_path)
        unclosed_frontmatter_prompt = """\
---
task_id: test-task
background: true

No closing delimiter here."""
        hook_input = _make_hook_input(
            "Agent",
            {"prompt": unclosed_frontmatter_prompt},
        )
        exit_code, stdout, stderr = _run_hook(hook_input)
        assert exit_code == 2, (
            f"Unclosed frontmatter should not be recognised → hard block expected. "
            f"got exit {exit_code}. stderr={stderr!r}"
        )


# ---------------------------------------------------------------------------
# Pure-function tests: _has_background_true_in_frontmatter
# ---------------------------------------------------------------------------
#
# These tests import the helper directly from the hook module to verify its
# contract in isolation, without the full hook execution path.
# ---------------------------------------------------------------------------


def _load_frontmatter_checker():
    """Import _has_background_true_in_frontmatter from the hook module.

    The hook is a script (not a library), so we compile and exec it in a
    restricted namespace that raises SystemExit early for the top-level
    statements that call sys.exit(). We only need the function definition.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("require_bg_module", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Inject a fake stdin so the module-level `json.load(sys.stdin)` can run.
    # The tool is not Agent, so execution exits at `sys.exit(0)` before
    # reaching the dispatcher check.
    import json
    from io import StringIO

    fake_stdin_data = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}
    )
    with (
        patch("sys.stdin", StringIO(fake_stdin_data)),
        patch("sys.stdout", StringIO()),
        patch("sys.stderr", StringIO()),
    ):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass

    return mod._has_background_true_in_frontmatter


class TestHasBackgroundTrueInFrontmatter:
    """Unit tests for the _has_background_true_in_frontmatter pure helper.

    These tests verify the parsing contract in isolation: given a prompt string,
    does the function correctly identify whether the YAML frontmatter contains
    `background: true` (or equivalent truthy value)?
    """

    @staticmethod
    def _fn():
        return _load_frontmatter_checker()

    def test_background_true_lowercase(self):
        fn = self._fn()
        assert fn("---\nbackground: true\n---\n\nbody") is True

    def test_background_True_python_style(self):
        fn = self._fn()
        assert fn("---\nbackground: True\n---\n\nbody") is True

    def test_background_yes(self):
        """background: yes is a truthy YAML value — must be accepted."""
        fn = self._fn()
        assert fn("---\nbackground: yes\n---\n\nbody") is True

    def test_background_1(self):
        """background: 1 is a truthy YAML value — must be accepted."""
        fn = self._fn()
        assert fn("---\nbackground: 1\n---\n\nbody") is True

    def test_background_false(self):
        fn = self._fn()
        assert fn("---\nbackground: false\n---\n\nbody") is False

    def test_background_key_absent(self):
        fn = self._fn()
        assert fn("---\ntask_id: x\nchat_id: 1\n---\n\nbody") is False

    def test_no_frontmatter(self):
        fn = self._fn()
        assert fn("Just a plain prompt.") is False

    def test_empty_string(self):
        fn = self._fn()
        assert fn("") is False

    def test_only_opening_delimiter(self):
        """A lone opening --- with no closing delimiter is not valid frontmatter."""
        fn = self._fn()
        assert fn("---\nbackground: true\n\nno closing") is False

    def test_leading_whitespace_stripped(self):
        """Prompts with leading newlines/spaces before --- are normalised."""
        fn = self._fn()
        assert fn("  \n---\nbackground: true\n---\n\nbody") is True

    def test_run_in_background_key_not_accepted(self):
        """run_in_background: true is the OLD key and must NOT be recognised as the sentinel.

        The sentinel key is `background`. Using run_in_background in the frontmatter
        is a mistake that should not silently pass — it will be stripped from tool_input
        and the frontmatter check will also correctly reject it.
        """
        fn = self._fn()
        assert fn("---\nrun_in_background: true\n---\n\nbody") is False

    def test_background_key_with_spaces_around_colon(self):
        """background : true (spaces around colon) should be handled by the line parser."""
        fn = self._fn()
        # The regex uses \s* around the colon, so spaces are accepted.
        assert fn("---\nbackground : true\n---\n\nbody") is True

    def test_background_after_other_keys(self):
        """background: true anywhere in the frontmatter block is accepted."""
        fn = self._fn()
        prompt = "---\ntask_id: my-task\nchat_id: 99\nsource: telegram\nbackground: true\n---\n\nbody"
        assert fn(prompt) is True
