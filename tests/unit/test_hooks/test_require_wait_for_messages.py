"""
Unit tests for hooks/require-wait-for-messages.py

Tests cover:
- Non-Lobster sessions (no LOBSTER_MAIN_SESSION=1) always exit 0
- Subagent sessions (not dispatcher) always exit 0
- Dispatcher session with wait_for_messages in transcript: exits 0 (allow)
- Dispatcher session without wait_for_messages in transcript: exits 2 (block)
- Missing transcript_path (I/O error): exits 0 to avoid false-positive block
- Success exit outputs JSON with suppressOutput=true to prevent feedback injection
- Block message references wait_for_messages to guide dispatcher behavior
- Graceful exit bypass: /tmp/lobster-graceful-exit flag allows stop without WFM
- Graceful exit flag is consumed (deleted) on use
- Block message documents the bypass mechanism
"""

import importlib.util
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOKS_DIR = Path(__file__).parents[3] / "hooks"
HOOK_PATH = HOOKS_DIR / "require-wait-for-messages.py"


def _load_hook(monkeypatch, tmp_path, is_dispatcher_result: bool = False):
    """Load require-wait-for-messages.py as a fresh module for each test.

    Patches session_role.is_dispatcher to return is_dispatcher_result so
    tests don't depend on the live dispatcher marker file.
    """
    monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

    spec = importlib.util.spec_from_file_location("require_wfm", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Insert hooks dir into path so session_role import works.
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))

    # Patch is_dispatcher before module execution so the module-level import
    # picks up the mock. We patch the session_role module that is already loaded.
    import session_role
    monkeypatch.setattr(session_role, "is_dispatcher", lambda data: is_dispatcher_result)

    spec.loader.exec_module(mod)
    return mod


def _make_jsonl_transcript(tool_names: list[str], tmp_path: Path) -> str:
    """Write a JSONL transcript file with the given tool_use names and return its path."""
    content = [
        {"type": "tool_use", "name": name, "id": f"id_{i}"}
        for i, name in enumerate(tool_names)
    ]
    entry = {
        "type": "assistant",
        "message": {"role": "assistant", "content": content},
    }
    tf = tmp_path / "transcript.jsonl"
    tf.write_text(json.dumps(entry) + "\n")
    return str(tf)


def _run_hook(mod, hook_input: dict) -> tuple[int, str, str]:
    """Run the hook's main() with the given input dict.

    Returns (exit_code, stdout, stderr).
    """
    stdout_buf = StringIO()
    stderr_buf = StringIO()
    with (
        patch("sys.stdin", StringIO(json.dumps(hook_input))),
        patch("sys.stdout", stdout_buf),
        patch("sys.stderr", stderr_buf),
    ):
        try:
            mod.main()
            code = 0
        except SystemExit as e:
            code = e.code
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests: session exemptions
# ---------------------------------------------------------------------------


def test_no_lobster_main_session_exits_0(monkeypatch, tmp_path):
    """Sessions without LOBSTER_MAIN_SESSION=1 are always allowed (exit 0)."""
    monkeypatch.delenv("LOBSTER_MAIN_SESSION", raising=False)

    spec = importlib.util.spec_from_file_location("require_wfm_noenv", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    spec.loader.exec_module(mod)

    hook_input = {"hook_event_name": "Stop", "session_id": "some-session"}
    code, stdout, _ = _run_hook(mod, hook_input)
    assert code == 0
    assert json.loads(stdout).get("suppressOutput") is True


def test_non_dispatcher_session_exits_0(monkeypatch, tmp_path):
    """Subagent sessions (is_dispatcher returns False) exit 0."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=False)
    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "subagent-session",
    }
    code, _, _ = _run_hook(mod, hook_input)
    assert code == 0


# ---------------------------------------------------------------------------
# Tests: dispatcher blocking behavior
# ---------------------------------------------------------------------------


def test_dispatcher_with_wfm_exits_0(monkeypatch, tmp_path):
    """Dispatcher that called wait_for_messages is allowed to stop (exit 0)."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(
        ["mcp__lobster-inbox__wait_for_messages"], tmp_path
    )
    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    code, _, _ = _run_hook(mod, hook_input)
    assert code == 0


def test_dispatcher_without_wfm_exits_2(monkeypatch, tmp_path):
    """Dispatcher that did NOT call wait_for_messages is blocked (exit 2)."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(
        ["mcp__lobster-inbox__mark_processed"],
        tmp_path,
    )
    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    code, _, _ = _run_hook(mod, hook_input)
    assert code == 2


def test_block_message_mentions_wfm(monkeypatch, tmp_path):
    """Block message tells dispatcher to call wait_for_messages."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(
        ["mcp__lobster-inbox__mark_processed"],
        tmp_path,
    )
    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    _, _, stderr = _run_hook(mod, hook_input)
    assert "wait_for_messages" in stderr.lower()


def test_dispatcher_with_wfm_also_in_transcript_exits_0(monkeypatch, tmp_path):
    """Dispatcher session with both mark_processed and wait_for_messages exits 0."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(
        [
            "mcp__lobster-inbox__mark_processed",
            "mcp__lobster-inbox__wait_for_messages",
        ],
        tmp_path,
    )
    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    code, _, _ = _run_hook(mod, hook_input)
    assert code == 0


# ---------------------------------------------------------------------------
# Tests: transcript read failure
# ---------------------------------------------------------------------------


def test_missing_transcript_path_exits_0(monkeypatch, tmp_path):
    """If transcript_path does not exist, allow stop to avoid false-positive block."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": str(tmp_path / "nonexistent.jsonl"),
    }
    code, _, _ = _run_hook(mod, hook_input)
    assert code == 0


# ---------------------------------------------------------------------------
# Tests: suppressOutput JSON
# ---------------------------------------------------------------------------


def test_success_exit_outputs_suppress_json(monkeypatch, tmp_path):
    """Successful exit (exit 0) outputs suppressOutput JSON to prevent CC feedback injection."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(
        ["mcp__lobster-inbox__wait_for_messages"], tmp_path
    )
    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    _, stdout, _ = _run_hook(mod, hook_input)
    output = json.loads(stdout)
    assert output.get("suppressOutput") is True


def test_non_lobster_session_outputs_suppress_json(monkeypatch, tmp_path):
    """Non-Lobster session also outputs suppressOutput JSON on exit 0."""
    monkeypatch.delenv("LOBSTER_MAIN_SESSION", raising=False)

    spec = importlib.util.spec_from_file_location("require_wfm_noenv2", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    spec.loader.exec_module(mod)

    hook_input = {"hook_event_name": "Stop", "session_id": "some-session"}
    _, stdout, _ = _run_hook(mod, hook_input)
    output = json.loads(stdout)
    assert output.get("suppressOutput") is True


# ---------------------------------------------------------------------------
# Tests: graceful exit bypass
# ---------------------------------------------------------------------------


def test_graceful_exit_flag_allows_stop(monkeypatch, tmp_path):
    """Dispatcher without WFM is allowed to stop when graceful exit flag is present."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(["mcp__lobster-inbox__mark_processed"], tmp_path)

    flag_path = tmp_path / "lobster-graceful-exit"
    flag_path.touch()

    monkeypatch.setattr(mod, "_GRACEFUL_EXIT_FLAG", str(flag_path))

    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    code, stdout, _ = _run_hook(mod, hook_input)
    assert code == 0
    assert json.loads(stdout).get("suppressOutput") is True


def test_graceful_exit_flag_is_consumed(monkeypatch, tmp_path):
    """Graceful exit flag file is deleted after use (single-use bypass)."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(["mcp__lobster-inbox__mark_processed"], tmp_path)

    flag_path = tmp_path / "lobster-graceful-exit"
    flag_path.touch()
    assert flag_path.exists()

    monkeypatch.setattr(mod, "_GRACEFUL_EXIT_FLAG", str(flag_path))

    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    _run_hook(mod, hook_input)
    assert not flag_path.exists(), "Flag file should be deleted after use"


def test_no_graceful_exit_flag_still_blocks(monkeypatch, tmp_path):
    """Without the flag file, dispatcher still gets blocked (exit 2)."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(["mcp__lobster-inbox__mark_processed"], tmp_path)

    # Point at a path that definitely doesn't exist.
    monkeypatch.setattr(mod, "_GRACEFUL_EXIT_FLAG", str(tmp_path / "no-such-flag"))

    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    code, _, _ = _run_hook(mod, hook_input)
    assert code == 2


def test_block_message_documents_bypass(monkeypatch, tmp_path):
    """Block message includes instructions for writing the graceful exit flag."""
    mod = _load_hook(monkeypatch, tmp_path, is_dispatcher_result=True)
    tf = _make_jsonl_transcript(["mcp__lobster-inbox__mark_processed"], tmp_path)

    monkeypatch.setattr(mod, "_GRACEFUL_EXIT_FLAG", str(tmp_path / "no-such-flag"))

    hook_input = {
        "hook_event_name": "Stop",
        "session_id": "dispatcher-session",
        "transcript_path": tf,
    }
    _, _, stderr = _run_hook(mod, hook_input)
    assert "lobster-graceful-exit" in stderr
