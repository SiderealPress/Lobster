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
- Spawn frontmatter must also carry chat_id and source; chat_id: 0 only with
  task_origin scheduled|internal. LOBSTER_SPAWN_FRONTMATTER_MODE=warn (default)
  logs and allows, =block exits 2 naming the key to add.
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


def _make_input(
    tool_name: str = "Agent", prompt: str = "", subagent_type: str | None = None
) -> dict:
    tool_input = {"prompt": prompt}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-test",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


@pytest.fixture(autouse=True)
def _isolated_spawn_frontmatter_env(tmp_path, monkeypatch):
    """Keep every test off the real warn log and independent of the ambient mode."""
    monkeypatch.delenv("LOBSTER_SPAWN_FRONTMATTER_MODE", raising=False)
    monkeypatch.setenv("LOBSTER_SPAWN_FRONTMATTER_WARN_LOG", str(tmp_path / "warn.log"))


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


# ---------------------------------------------------------------------------
# Spawn frontmatter: chat_id / source / task_origin
# ---------------------------------------------------------------------------

_MODE_ENV = "LOBSTER_SPAWN_FRONTMATTER_MODE"
_WARN_LOG_ENV = "LOBSTER_SPAWN_FRONTMATTER_WARN_LOG"

_COMPLETE_USER_PROMPT = (
    "---\ntask_id: user-fixture-a\nchat_id: 1000001\nsource: telegram\n---\nDo work."
)
_MISSING_CHAT_ID_PROMPT = "---\ntask_id: user-fixture-b\nsource: telegram\n---\nDo work."


def _warn_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / "warn.log"
    return log.read_text().splitlines() if log.exists() else []


class TestSpawnFrontmatterChatId:
    def test_block_mode_rejects_missing_chat_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_MODE_ENV, "block")
        exit_code, stdout, stderr = _run_hook(
            _make_input(prompt=_MISSING_CHAT_ID_PROMPT, subagent_type="general-purpose")
        )
        assert exit_code == 2
        assert "BLOCKED" in stderr
        assert "chat_id" in stderr
        assert "BLOCKED" not in stdout
        assert _warn_lines(tmp_path) == []

    def test_block_mode_names_missing_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_MODE_ENV, "block")
        prompt = "---\ntask_id: user-fixture-c\nchat_id: 1000001\n---\nDo work."
        exit_code, _, stderr = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 2
        assert "[source]" in stderr
        assert "[chat_id]" not in stderr

    def test_block_mode_allows_complete_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_MODE_ENV, "block")
        exit_code, _, stderr = _run_hook(_make_input(prompt=_COMPLETE_USER_PROMPT))
        assert exit_code == 0
        assert stderr == ""
        assert _warn_lines(tmp_path) == []

    def test_block_mode_rejects_legacy_text_without_frontmatter(self, monkeypatch):
        """Legacy 'task_id is:' still satisfies the task_id rule, but carries no chat_id."""
        monkeypatch.setenv(_MODE_ENV, "block")
        exit_code, _, stderr = _run_hook(_make_input(prompt="Your task_id is: legacy-fixture"))
        assert exit_code == 2
        assert "chat_id" in stderr

    def test_warn_mode_logs_and_allows(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_MODE_ENV, "warn")
        exit_code, stdout, stderr = _run_hook(
            _make_input(prompt=_MISSING_CHAT_ID_PROMPT, subagent_type="general-purpose")
        )
        assert exit_code == 0
        lines = _warn_lines(tmp_path)
        assert len(lines) == 1
        assert "general-purpose" in lines[0]
        assert "chat_id" in lines[0]
        assert "source" not in lines[0].split("missing=", 1)[-1]
        assert "chat_id" in stderr
        assert stdout == ""

    def test_warn_mode_complete_frontmatter_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_MODE_ENV, "warn")
        exit_code, _, stderr = _run_hook(_make_input(prompt=_COMPLETE_USER_PROMPT))
        assert exit_code == 0
        assert stderr == ""
        assert _warn_lines(tmp_path) == []

    def test_default_mode_is_warn(self, tmp_path):
        """With the env key unset, a spawn missing chat_id is allowed but logged."""
        exit_code, _, _ = _run_hook(_make_input(prompt=_MISSING_CHAT_ID_PROMPT))
        assert exit_code == 0
        assert len(_warn_lines(tmp_path)) == 1

    def test_unknown_mode_behaves_as_warn(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_MODE_ENV, "strict-ish")
        exit_code, _, _ = _run_hook(_make_input(prompt=_MISSING_CHAT_ID_PROMPT))
        assert exit_code == 0
        assert len(_warn_lines(tmp_path)) == 1

    def test_default_warn_log_path_is_under_workspace_logs(self, tmp_path, monkeypatch):
        monkeypatch.delenv(_WARN_LOG_ENV, raising=False)
        monkeypatch.delenv("LOBSTER_WORKSPACE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        exit_code, _, _ = _run_hook(_make_input(prompt=_MISSING_CHAT_ID_PROMPT))
        assert exit_code == 0
        default_log = tmp_path / "lobster-workspace" / "logs" / "spawn-frontmatter-warn.log"
        assert default_log.exists()
        assert "chat_id" in default_log.read_text()

    @pytest.mark.parametrize("origin_line,expected_exit", [
        ("", 2),
        ("task_origin: user\n", 2),
        ("task_origin: internal\n", 0),
        ("task_origin: scheduled\n", 0),
    ])
    def test_chat_id_zero_requires_internal_origin(
        self, monkeypatch, origin_line, expected_exit
    ):
        """chat_id: 0 is only valid for non-user work (task_origin scheduled|internal)."""
        monkeypatch.setenv(_MODE_ENV, "block")
        prompt = (
            "---\ntask_id: internal-fixture-a\nchat_id: 0\nsource: system\n"
            f"{origin_line}---\nDo work."
        )
        exit_code, _, stderr = _run_hook(_make_input(prompt=prompt))
        assert exit_code == expected_exit
        if expected_exit == 2:
            assert "task_origin" in stderr

    def test_missing_task_id_still_blocks_regardless_of_mode(self, monkeypatch):
        monkeypatch.setenv(_MODE_ENV, "warn")
        prompt = "---\nchat_id: 1000001\nsource: telegram\n---\nDo work."
        exit_code, _, stderr = _run_hook(_make_input(prompt=prompt))
        assert exit_code == 2
        assert "task_id" in stderr


# ---------------------------------------------------------------------------
# spawn_frontmatter: pure decision functions
# ---------------------------------------------------------------------------

def _load_spawn_frontmatter():
    import importlib.util
    path = _HOOKS_DIR / "spawn_frontmatter.py"
    spec = importlib.util.spec_from_file_location("spawn_frontmatter", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sf = _load_spawn_frontmatter()


class TestSpawnFrontmatterPure:
    @pytest.mark.parametrize("fm,expected", [
        ({"chat_id": "1000001", "source": "telegram"}, ()),
        ({"source": "telegram"}, ("chat_id",)),
        ({"chat_id": "1000001"}, ("source",)),
        ({}, ("chat_id", "source")),
        ({"chat_id": "0", "source": "system"}, ("task_origin",)),
        ({"chat_id": "0", "source": "system", "task_origin": "user"}, ("task_origin",)),
        ({"chat_id": "0", "source": "system", "task_origin": "Internal"}, ()),
        ({"chat_id": "0", "source": "system", "task_origin": "scheduled"}, ()),
    ])
    def test_find_problems(self, fm, expected):
        assert sf.find_problems(fm) == expected

    def test_parse_frontmatter_unquotes_values(self):
        fm = sf.parse_frontmatter('---\nchat_id: "0"\nsource: \'system\'\n---\nbody')
        assert fm == {"chat_id": "0", "source": "system"}

    def test_parse_frontmatter_requires_closed_block(self):
        assert sf.parse_frontmatter("---\nchat_id: 1000001\n") == {}

    @pytest.mark.parametrize("raw,expected", [
        (None, "warn"), ("", "warn"), ("warn", "warn"), ("BLOCK", "block"), ("off", "warn"),
    ])
    def test_resolve_mode(self, raw, expected):
        assert sf.resolve_mode(raw) == expected

    def test_evaluate_spawn_blocked_only_in_block_mode(self):
        prompt = "---\ntask_id: t\n---\n"
        assert sf.evaluate_spawn(prompt, "block").blocked
        assert not sf.evaluate_spawn(prompt, "warn").blocked
        assert sf.evaluate_spawn(prompt, "warn").warn

    def test_resolve_warn_log_path_precedence(self, tmp_path):
        home = tmp_path / "home"
        assert sf.resolve_warn_log_path({}, home) == (
            home / "lobster-workspace" / "logs" / "spawn-frontmatter-warn.log"
        )
        assert sf.resolve_warn_log_path({"LOBSTER_WORKSPACE": "/ws"}, home) == Path(
            "/ws/logs/spawn-frontmatter-warn.log"
        )
        assert sf.resolve_warn_log_path(
            {"LOBSTER_WORKSPACE": "/ws", sf.WARN_LOG_ENV: "/x/w.log"}, home
        ) == Path("/x/w.log")

    def test_warn_line_format(self):
        line = sf.format_warn_line("2000-01-01T00:00:00Z", None, ("chat_id", "source"))
        assert line == "2000-01-01T00:00:00Z subagent_type=- missing=chat_id,source"
