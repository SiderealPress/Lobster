"""
Unit tests for hooks/block-claude-p.py

Tests cover:
- Bash invocation with `claude -p` → should fire
- Bash invocation with `claude --print` → should fire
- Shell comment `# claude -p foo` → should NOT fire
- String literal assignment `msg="run: claude -p"` → should NOT fire (heuristic)
- Known-safe callers (run-job.sh, claude-persistent.sh) → should NOT fire
- Non-Bash tools (Write, Edit) → should NOT fire
- warn mode: exits 0 with stdout message
- block mode: exits 2 with stderr message
- Log file written on match
- No log file written when no match
"""
import importlib.util
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "block-claude-p.py"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_hook(monkeypatch, tmp_path, mode: str = "warn"):
    """Load block-claude-p.py as a fresh module with an isolated log file."""
    monkeypatch.setenv("LOBSTER_BLOCK_CLAUDE_P_MODE", mode)
    monkeypatch.setenv("HOME", str(tmp_path))

    spec = importlib.util.spec_from_file_location("block_claude_p", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Override the log file path to a temp location.
    log_file = tmp_path / "logs" / "claude-p-blocks.jsonl"
    monkeypatch.setattr(mod, "_LOG_FILE", log_file)

    return mod, log_file


def _make_bash_input(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _make_other_tool_input(tool_name: str, command: str) -> dict:
    return {"tool_name": tool_name, "tool_input": {"command": command}}


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    """Run mod.main() with hook_input as stdin JSON."""
    stdout_capture = StringIO()
    stderr_capture = StringIO()
    stdin_data = json.dumps(hook_input)

    exit_code = None
    with patch("sys.stdin", StringIO(stdin_data)), \
         patch("sys.stdout", stdout_capture), \
         patch("sys.stderr", stderr_capture):
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()


# ---------------------------------------------------------------------------
# Detection tests (warn mode — should fire but allow)
# ---------------------------------------------------------------------------

class TestDetection:
    def test_claude_dash_p_fires(self, monkeypatch, tmp_path):
        """Basic `claude -p 'prompt'` → hook fires."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("claude -p 'summarise this'")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "warn mode should exit 0"
        assert "warn" in stdout.lower() or "block-claude-p" in stdout.lower()

    def test_claude_print_fires(self, monkeypatch, tmp_path):
        """Long form `claude --print 'prompt'` → hook fires."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("claude --print 'summarise this'")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0, "warn mode should exit 0"
        assert "warn" in stdout.lower() or "block-claude-p" in stdout.lower()

    def test_claude_p_with_model_flag_fires(self, monkeypatch, tmp_path):
        """Typical `claude -p --model claude-opus-4 'prompt'` → hook fires."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("claude -p --model claude-opus-4 'do work'")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert "warn" in stdout.lower() or "block-claude-p" in stdout.lower()

    def test_no_match_no_output(self, monkeypatch, tmp_path):
        """Bash command without claude -p → exits 0 silently."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("ls -la ~/messages/inbox/")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == ""
        assert stderr == ""


# ---------------------------------------------------------------------------
# Allowlist tests (should NOT fire)
# ---------------------------------------------------------------------------

class TestAllowlist:
    def test_comment_line_not_fired(self, monkeypatch, tmp_path):
        """Shell comment `# claude -p foo` → should NOT fire."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("# claude -p summarise the output above")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == "", f"Comment line should not trigger hook, got: {stdout!r}"

    def test_comment_line_with_leading_spaces_not_fired(self, monkeypatch, tmp_path):
        """Indented comment `  # claude -p foo` → should NOT fire."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("  # claude -p echo")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == ""

    def test_string_literal_not_fired(self, monkeypatch, tmp_path):
        """Assignment `msg="run: claude -p"` → heuristic suppresses match."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input('msg="example: claude -p prompt"')
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == "", f"String literal should not trigger hook, got: {stdout!r}"

    def test_run_job_sh_not_fired(self, monkeypatch, tmp_path):
        """run-job.sh caller → allowlisted, should NOT fire."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("bash ~/lobster/scheduled-tasks/run-job.sh claude -p 'task'")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == "", f"run-job.sh should be allowlisted, got: {stdout!r}"

    def test_claude_persistent_sh_not_fired(self, monkeypatch, tmp_path):
        """claude-persistent.sh caller → allowlisted, should NOT fire."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("~/lobster/scripts/claude-persistent.sh -p 'start session'")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == "", f"claude-persistent.sh should be allowlisted, got: {stdout!r}"


# ---------------------------------------------------------------------------
# Non-Bash tool tests (should NOT fire)
# ---------------------------------------------------------------------------

class TestNonBashTool:
    def test_write_tool_not_fired(self, monkeypatch, tmp_path):
        """Write tool with `claude -p` in content → should NOT fire (Bash only)."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_other_tool_input("Write", "claude -p 'summarise'")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == ""

    def test_edit_tool_not_fired(self, monkeypatch, tmp_path):
        """Edit tool with `claude -p` in content → should NOT fire."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_other_tool_input("Edit", "claude -p 'summarise'")
        exit_code, stdout, _ = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout == ""


# ---------------------------------------------------------------------------
# Mode tests
# ---------------------------------------------------------------------------

class TestBlockMode:
    def test_block_mode_exits_2(self, monkeypatch, tmp_path):
        """block mode: `claude -p` → exits 2 (hard block)."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="block")
        hook_input = _make_bash_input("claude -p 'do work'")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)
        assert exit_code == 2, f"block mode should exit 2, got {exit_code}"
        assert "BLOCKED" in stderr or "claude -p" in stderr.lower()

    def test_block_mode_block_message_in_stderr(self, monkeypatch, tmp_path):
        """block mode: block message goes to stderr, not stdout."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="block")
        hook_input = _make_bash_input("claude -p 'do work'")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)
        assert exit_code == 2
        assert "BLOCKED" in stderr
        assert "BLOCKED" not in stdout

    def test_warn_mode_exits_0_with_message(self, monkeypatch, tmp_path):
        """warn mode: `claude -p` → exits 0 with stdout warning."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("claude -p 'do work'")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)
        assert exit_code == 0
        assert stdout.strip() != "", "warn mode should print a message to stdout"
        assert stderr == "", "warn mode should not print to stderr"

    def test_allowlist_still_works_in_block_mode(self, monkeypatch, tmp_path):
        """block mode: known-safe callers still pass through."""
        mod, _ = _load_hook(monkeypatch, tmp_path, mode="block")
        hook_input = _make_bash_input("bash run-job.sh claude -p 'task prompt'")
        exit_code, stdout, stderr = _run_hook(mod, hook_input)
        assert exit_code == 0, f"Allowlisted caller should exit 0 even in block mode"


# ---------------------------------------------------------------------------
# Logging tests
# ---------------------------------------------------------------------------

class TestLogging:
    def test_log_written_on_match(self, monkeypatch, tmp_path):
        """Matches are appended to the JSONL log file."""
        mod, log_file = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("claude -p 'summarise'")
        _run_hook(mod, hook_input)
        assert log_file.exists(), "Log file should be created on match"
        lines = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        entry = lines[0]
        assert entry["mode"] == "warn"
        assert entry["tool"] == "Bash"
        assert "matches" in entry
        assert len(entry["matches"]) > 0

    def test_log_not_written_when_no_match(self, monkeypatch, tmp_path):
        """No log entry when there is no match."""
        mod, log_file = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("ls -la")
        _run_hook(mod, hook_input)
        assert not log_file.exists(), "Log file should not be created when no match"

    def test_log_not_written_for_allowlisted_call(self, monkeypatch, tmp_path):
        """Allowlisted callers do not produce log entries."""
        mod, log_file = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("bash run-job.sh claude -p 'prompt'")
        _run_hook(mod, hook_input)
        assert not log_file.exists(), "Allowlisted callers should not log"

    def test_log_entry_contains_timestamp(self, monkeypatch, tmp_path):
        """Log entries include an ISO-8601 timestamp."""
        mod, log_file = _load_hook(monkeypatch, tmp_path, mode="warn")
        hook_input = _make_bash_input("claude -p 'test'")
        _run_hook(mod, hook_input)
        entry = json.loads(log_file.read_text().splitlines()[0])
        assert "timestamp" in entry
        # Should be parseable as ISO-8601
        from datetime import datetime
        datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))

    def test_log_appends_multiple_entries(self, monkeypatch, tmp_path):
        """Multiple matches append multiple log entries."""
        mod, log_file = _load_hook(monkeypatch, tmp_path, mode="warn")
        for i in range(3):
            hook_input = _make_bash_input(f"claude -p 'run {i}'")
            _run_hook(mod, hook_input)

        lines = [l for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 3, f"Expected 3 log entries, got {len(lines)}"


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    def test_is_comment_line(self, monkeypatch, tmp_path):
        mod, _ = _load_hook(monkeypatch, tmp_path)
        assert mod._is_comment_line("# claude -p")
        assert mod._is_comment_line("   # indented")
        assert not mod._is_comment_line("echo foo")
        assert not mod._is_comment_line("claude -p 'real'")

    def test_contains_safe_caller(self, monkeypatch, tmp_path):
        mod, _ = _load_hook(monkeypatch, tmp_path)
        assert mod._contains_safe_caller("bash run-job.sh claude -p 'x'")
        assert mod._contains_safe_caller("~/scripts/claude-persistent.sh -p")
        assert not mod._contains_safe_caller("claude -p 'unknown caller'")

    def test_find_matches_returns_empty_for_no_match(self, monkeypatch, tmp_path):
        mod, _ = _load_hook(monkeypatch, tmp_path)
        assert mod._find_matches_in_command("ls -la") == []
        assert mod._find_matches_in_command("# claude -p comment") == []

    def test_find_matches_returns_matches_for_real_invocation(self, monkeypatch, tmp_path):
        mod, _ = _load_hook(monkeypatch, tmp_path)
        matches = mod._find_matches_in_command("claude -p 'prompt here'")
        assert len(matches) > 0

    def test_find_matches_allowlists_safe_callers(self, monkeypatch, tmp_path):
        mod, _ = _load_hook(monkeypatch, tmp_path)
        assert mod._find_matches_in_command("bash run-job.sh claude -p 'x'") == []
