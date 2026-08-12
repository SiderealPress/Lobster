"""
Unit tests for hooks/deferred-commitment-guard.py

Tests cover:
- PostToolUse on send_reply: deferral language -> sentinel written (dispatcher only)
- PostToolUse on send_reply: non-deferral text -> no sentinel written
- PostToolUse on send_reply: non-dispatcher session -> no sentinel written
- PostToolUse on create_task: DEFERRED: subject -> sentinel cleared
- PostToolUse on create_task: non-DEFERRED subject -> sentinel left alone
- PreToolUse: no sentinel -> silent, exit 0
- PreToolUse: sentinel + next tool is create_task -> silent, sentinel left for
  the PostToolUse create_task handler to evaluate
- PreToolUse: sentinel + next tool is something else -> warns once, clears sentinel,
  exit 0 (never blocks)
- PreToolUse: subagent (agent_id present) -> passes through untouched
- Fail-open: malformed stdin JSON -> exit 0, no crash
"""

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "deferred-commitment-guard.py"


def _load_hook(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
    sys.path.insert(0, str(HOOKS_DIR))
    spec = importlib.util.spec_from_file_location("deferred_commitment_guard", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(hook_mod, stdin_data: dict):
    stdin_str = json.dumps(stdin_data)
    captured_stderr = StringIO()
    exit_code = None
    with patch("sys.stdin", StringIO(stdin_str)), patch("sys.stderr", captured_stderr):
        try:
            hook_mod.main()
        except SystemExit as e:
            exit_code = e.code
    return exit_code, captured_stderr.getvalue()


def _sentinel_path(mod) -> Path:
    return mod.SENTINEL_PATH


class TestPostToolUseSendReply:
    def test_deferral_language_writes_sentinel_for_dispatcher(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)

        exit_code, _ = _run_main(mod, {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__lobster-inbox__send_reply",
            "tool_input": {"text": "I'll check on that and get back to you."},
        })

        assert exit_code == 0
        assert _sentinel_path(mod).exists()
        payload = json.loads(_sentinel_path(mod).read_text())
        assert "check on that" in payload["reply_snippet"]

    def test_non_deferral_text_does_not_write_sentinel(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: True)

        exit_code, _ = _run_main(mod, {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__lobster-inbox__send_reply",
            "tool_input": {"text": "Here's the answer: it's 42."},
        })

        assert exit_code == 0
        assert not _sentinel_path(mod).exists()

    def test_non_dispatcher_session_does_not_write_sentinel(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        monkeypatch.setattr(mod, "is_dispatcher_session", lambda _data: False)

        exit_code, _ = _run_main(mod, {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__lobster-inbox__send_reply",
            "tool_input": {"text": "I'll check on that."},
        })

        assert exit_code == 0
        assert not _sentinel_path(mod).exists()


class TestPostToolUseCreateTask:
    def test_deferred_subject_clears_sentinel(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        _sentinel_path(mod).parent.mkdir(parents=True, exist_ok=True)
        _sentinel_path(mod).write_text(json.dumps({"ts": "x", "reply_snippet": "I'll check"}))

        exit_code, _ = _run_main(mod, {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__lobster-inbox__create_task",
            "tool_input": {"subject": "DEFERRED: what time is the meeting"},
        })

        assert exit_code == 0
        assert not _sentinel_path(mod).exists()

    def test_non_deferred_subject_leaves_sentinel(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        _sentinel_path(mod).parent.mkdir(parents=True, exist_ok=True)
        _sentinel_path(mod).write_text(json.dumps({"ts": "x", "reply_snippet": "I'll check"}))

        exit_code, _ = _run_main(mod, {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__lobster-inbox__create_task",
            "tool_input": {"subject": "Buy groceries"},
        })

        assert exit_code == 0
        assert _sentinel_path(mod).exists()


class TestPreToolUse:
    def test_no_sentinel_silent(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)

        exit_code, stderr = _run_main(mod, {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {},
        })

        assert exit_code == 0
        assert stderr == ""

    def test_sentinel_present_next_is_create_task_silent(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        _sentinel_path(mod).parent.mkdir(parents=True, exist_ok=True)
        _sentinel_path(mod).write_text(json.dumps({"ts": "x", "reply_snippet": "I'll check"}))

        exit_code, stderr = _run_main(mod, {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__lobster-inbox__create_task",
            "tool_input": {},
        })

        assert exit_code == 0
        assert stderr == ""
        # Left for the PostToolUse create_task handler to evaluate/clear.
        assert _sentinel_path(mod).exists()

    def test_sentinel_present_next_is_other_tool_warns_and_clears(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        _sentinel_path(mod).parent.mkdir(parents=True, exist_ok=True)
        _sentinel_path(mod).write_text(json.dumps({"ts": "x", "reply_snippet": "I'll check on that"}))

        exit_code, stderr = _run_main(mod, {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__lobster-inbox__wait_for_messages",
            "tool_input": {},
        })

        assert exit_code == 0  # never blocks
        assert "deferred-commitment-guard" in stderr
        assert "I'll check on that" in stderr
        assert not _sentinel_path(mod).exists()

    def test_subagent_passes_through_untouched(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        _sentinel_path(mod).parent.mkdir(parents=True, exist_ok=True)
        _sentinel_path(mod).write_text(json.dumps({"ts": "x", "reply_snippet": "I'll check"}))

        exit_code, stderr = _run_main(mod, {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {},
            "agent_id": "some-subagent-id",
        })

        assert exit_code == 0
        assert stderr == ""
        # Subagent tool calls don't consume/clear the dispatcher's sentinel.
        assert _sentinel_path(mod).exists()


class TestFailOpen:
    def test_malformed_stdin_exits_zero(self, monkeypatch, tmp_path):
        mod = _load_hook(monkeypatch, tmp_path)
        captured_stderr = StringIO()
        exit_code = None
        with patch("sys.stdin", StringIO("not json")), patch("sys.stderr", captured_stderr):
            try:
                mod.main()
            except SystemExit as e:
                exit_code = e.code
        assert exit_code == 0
