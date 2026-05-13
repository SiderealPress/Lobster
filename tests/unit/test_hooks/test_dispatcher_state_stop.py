"""
Unit tests for hooks/dispatcher-state-stop.py — agent_id guard (issue #1958).

## What this file tests

The Stop hook must write DEAD state + context-handoff.json only for the
dispatcher session, and skip silently for subagents. The correct detection
mechanism is the agent_id field (PR #2007 approach):

  - agent_id absent or empty  → dispatcher → proceed
  - agent_id present           → subagent  → exit 0, no writes

This replaces the previous is_dispatcher() / is_dispatcher_session() calls
which were unreliable at SessionStop time:
  - is_dispatcher() reads the startup-flag file, deleted at SessionStart
  - is_dispatcher_session() adds ~10ms subprocess overhead unnecessarily

## Named constants (spec-derived)

  AGENT_ID_FIELD = "agent_id"      # field name injected by CC into subagent payloads
  SUBAGENT_AGENT_ID = "abc-123"    # any non-empty string = subagent
  DISPATCHER_HAS_NO_AGENT_ID = True  # dispatcher payloads never carry agent_id

## Behaviors verified

Agent_id guard:
  1. Subagent (agent_id present) → no DEAD state, no handoff, exit 0
  2. Dispatcher (agent_id absent) → DEAD state written, handoff written
  3. Dispatcher (agent_id empty string) → treated as dispatcher (fail-open)
  4. Dispatcher (agent_id None) → treated as dispatcher (fail-open)
  5. Malformed stdin → hook_input is {} → treated as dispatcher (fail-open)
  6. is_subagent() pure function contract

DEAD state writes:
  7. DEAD state written for dispatcher session
  8. DEAD state not written for subagent session

Handoff writes (delegated to existing test coverage — tests here verify the
guard decision only; full handoff content tests are in the shared helper tests):
  9. Handoff not written for subagent
  10. Handoff written for dispatcher

No session_role import:
  11. AST-level check: is_dispatcher and is_dispatcher_session must not be called
"""

import ast
import importlib.util
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Spec-derived named constants
# ---------------------------------------------------------------------------

# The hook payload field that Claude Code injects only into subagent payloads.
AGENT_ID_FIELD = "agent_id"

# Any non-empty agent_id value identifies a subagent.
SUBAGENT_AGENT_ID = "subagent-abc-123"

# Named constant documenting the dispatcher invariant.
DISPATCHER_HAS_NO_AGENT_ID = True

# Expected hook behaviour constants.
SUBAGENT_MUST_NOT_WRITE_DEAD_STATE = True
DISPATCHER_MUST_WRITE_DEAD_STATE = True
HANDOFF_FILENAME = "context-handoff.json"


# ---------------------------------------------------------------------------
# Module loader helpers
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "dispatcher-state-stop.py"


def _load_hook():
    """Load dispatcher-state-stop.py as a fresh module.

    Inserts src/ into sys.path so state_machine can be imported.
    """
    src_dir = _HOOKS_DIR.parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    spec = importlib.util.spec_from_file_location("dispatcher_state_stop", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, monkeypatch, hook_input: dict, workspace: Path) -> tuple[int, list]:
    """Call mod.main() with patched stdin, workspace env, and tracked state_machine calls.

    Returns (exit_code, write_state_calls) where write_state_calls is the list of
    positional/keyword args passed to state_machine.write_state().
    """
    stdin_json = json.dumps(hook_input)
    monkeypatch.setattr(sys, "stdin", StringIO(stdin_json))
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))

    # Ensure workspace/data/ exists for handoff writes.
    (workspace / "data").mkdir(parents=True, exist_ok=True)

    import state_machine as sm

    write_state_calls: list[tuple] = []

    def tracking_write_state(*args, **kwargs):
        write_state_calls.append((args, kwargs))

    monkeypatch.setattr(sm, "write_state", tracking_write_state)

    exit_code = 0
    try:
        mod.main()
    except SystemExit as e:
        exit_code = e.code or 0
    return exit_code, write_state_calls


# ---------------------------------------------------------------------------
# Tests: is_subagent() pure function
# ---------------------------------------------------------------------------


class TestIsSubagent:
    """is_subagent() must correctly classify dispatcher vs subagent hook inputs."""

    def test_subagent_when_agent_id_present(self):
        """Non-empty agent_id → subagent."""
        mod = _load_hook()
        assert mod.is_subagent({AGENT_ID_FIELD: SUBAGENT_AGENT_ID}) is True

    def test_dispatcher_when_agent_id_absent(self):
        """Missing agent_id key → dispatcher (fail-open)."""
        mod = _load_hook()
        assert mod.is_subagent({}) is False

    def test_dispatcher_when_agent_id_empty_string(self):
        """Empty string agent_id → treated as dispatcher (fail-open)."""
        mod = _load_hook()
        assert mod.is_subagent({AGENT_ID_FIELD: ""}) is False

    def test_dispatcher_when_agent_id_none(self):
        """None agent_id → treated as dispatcher (fail-open)."""
        mod = _load_hook()
        assert mod.is_subagent({AGENT_ID_FIELD: None}) is False

    def test_dispatcher_with_session_id_only(self):
        """Payload with session_id but no agent_id → dispatcher."""
        mod = _load_hook()
        assert mod.is_subagent({"session_id": "aaaa-bbbb-cccc"}) is False

    def test_subagent_with_both_fields(self):
        """Payload with both agent_id and session_id → subagent."""
        mod = _load_hook()
        assert mod.is_subagent(
            {AGENT_ID_FIELD: SUBAGENT_AGENT_ID, "session_id": "some-id"}
        ) is True

    def test_agent_id_field_constant_used(self):
        """AGENT_ID_FIELD constant must equal 'agent_id' (prevents silent rename drift)."""
        mod = _load_hook()
        assert mod.AGENT_ID_FIELD == "agent_id", (
            f"AGENT_ID_FIELD must be 'agent_id', got {mod.AGENT_ID_FIELD!r}"
        )


# ---------------------------------------------------------------------------
# Tests: subagent sessions skip all writes
# ---------------------------------------------------------------------------


class TestSubagentSkipsAllWrites:
    """Stop hook must exit 0 with no writes for subagent sessions."""

    def test_subagent_does_not_write_dead_state(self, monkeypatch, tmp_path):
        """DEAD state write must be skipped for subagent sessions."""
        mod = _load_hook()
        hook_input = {AGENT_ID_FIELD: SUBAGENT_AGENT_ID, "session_id": "sub-session"}
        exit_code, write_calls = _run_main(mod, monkeypatch, hook_input, tmp_path)

        assert exit_code == 0, "Hook must exit 0 for subagent"
        assert write_calls == [], (
            f"state_machine.write_state must NOT be called for subagent; got {write_calls}"
        )

    def test_subagent_does_not_write_handoff(self, monkeypatch, tmp_path):
        """context-handoff.json must NOT be written for subagent sessions."""
        mod = _load_hook()
        hook_input = {AGENT_ID_FIELD: SUBAGENT_AGENT_ID, "session_id": "sub-session"}
        _run_main(mod, monkeypatch, hook_input, tmp_path)

        handoff_path = tmp_path / "data" / HANDOFF_FILENAME
        assert not handoff_path.exists(), (
            f"context-handoff.json must NOT be written for subagent sessions"
        )


# ---------------------------------------------------------------------------
# Tests: dispatcher session proceeds with all writes
# ---------------------------------------------------------------------------


class TestDispatcherProceedsWithWrites:
    """Stop hook must write DEAD state and context-handoff.json for dispatcher."""

    def test_dispatcher_writes_dead_state(self, monkeypatch, tmp_path):
        """state_machine.write_state must be called for dispatcher sessions."""
        mod = _load_hook()
        hook_input = {"session_id": "disp-session"}  # no agent_id
        exit_code, write_calls = _run_main(mod, monkeypatch, hook_input, tmp_path)

        assert exit_code == 0, "Hook must exit 0 for dispatcher"
        assert len(write_calls) == 1, (
            f"state_machine.write_state must be called once for dispatcher; got {write_calls}"
        )

    def test_dispatcher_writes_handoff(self, monkeypatch, tmp_path):
        """context-handoff.json must be written for dispatcher sessions."""
        mod = _load_hook()
        hook_input = {"session_id": "disp-session"}  # no agent_id
        _run_main(mod, monkeypatch, hook_input, tmp_path)

        handoff_path = tmp_path / "data" / HANDOFF_FILENAME
        assert handoff_path.exists(), (
            "context-handoff.json must be written for dispatcher sessions"
        )

    def test_dispatcher_empty_agent_id_treated_as_dispatcher(self, monkeypatch, tmp_path):
        """agent_id='' (empty string) is treated as dispatcher — fail-open."""
        mod = _load_hook()
        hook_input = {AGENT_ID_FIELD: "", "session_id": "disp-session"}
        exit_code, write_calls = _run_main(mod, monkeypatch, hook_input, tmp_path)

        assert exit_code == 0
        assert len(write_calls) == 1, (
            "Empty agent_id must be treated as dispatcher — DEAD state must be written"
        )

    def test_dispatcher_none_agent_id_treated_as_dispatcher(self, monkeypatch, tmp_path):
        """agent_id=None is treated as dispatcher — fail-open."""
        mod = _load_hook()
        hook_input = {AGENT_ID_FIELD: None, "session_id": "disp-session"}
        exit_code, write_calls = _run_main(mod, monkeypatch, hook_input, tmp_path)

        assert exit_code == 0
        assert len(write_calls) == 1, (
            "None agent_id must be treated as dispatcher — DEAD state must be written"
        )


# ---------------------------------------------------------------------------
# Tests: fail-open on malformed stdin
# ---------------------------------------------------------------------------


class TestFailOpenOnMalformedStdin:
    """Hook must fail open (treat as dispatcher) when stdin is unparseable."""

    def test_malformed_stdin_treated_as_dispatcher(self, monkeypatch, tmp_path):
        """Unparseable stdin → hook_input={} → no agent_id → treated as dispatcher."""
        mod = _load_hook()

        monkeypatch.setattr(sys, "stdin", StringIO("not valid json {{{"))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        import state_machine as sm
        write_calls = []
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: write_calls.append((a, kw)))

        exit_code = 0
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code or 0

        assert exit_code == 0, "Hook must exit 0 on malformed stdin"
        assert len(write_calls) == 1, (
            "Malformed stdin must fail open: DEAD state must be written (cannot be a subagent)"
        )

    def test_empty_stdin_treated_as_dispatcher(self, monkeypatch, tmp_path):
        """Empty stdin → hook_input={} → no agent_id → treated as dispatcher."""
        mod = _load_hook()

        monkeypatch.setattr(sys, "stdin", StringIO(""))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path))
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        import state_machine as sm
        write_calls = []
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: write_calls.append((a, kw)))

        exit_code = 0
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code or 0

        assert exit_code == 0, "Hook must exit 0 on empty stdin"
        assert len(write_calls) == 1, (
            "Empty stdin must fail open: DEAD state must be written"
        )


# ---------------------------------------------------------------------------
# Tests: no session_role dependency (AST-level)
# ---------------------------------------------------------------------------

# Named constants for the functions that must NOT be called (issue #1958 spec).
FORBIDDEN_CALL_IS_DISPATCHER = "is_dispatcher"
FORBIDDEN_CALL_IS_DISPATCHER_SESSION = "is_dispatcher_session"
FORBIDDEN_IMPORT_SESSION_ROLE = "session_role"


class TestNoSessionRoleDependency:
    """Hook must not import session_role or call is_dispatcher/is_dispatcher_session.

    These functions are unreliable at SessionStop time:
    - is_dispatcher() reads the startup-flag file, deleted before SessionStop fires.
    - is_dispatcher_session() adds ~10ms subprocess overhead (process-tree walk).

    The agent_id guard eliminates this dependency entirely.
    """

    def _parse_hook_ast(self) -> ast.Module:
        """Parse dispatcher-state-stop.py into an AST."""
        return ast.parse(_HOOK_PATH.read_text())

    def _collect_import_names(self, tree: ast.Module) -> set[str]:
        """Collect all imported module names from the AST."""
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def _collect_call_names(self, tree: ast.Module) -> set[str]:
        """Collect all function call names (including dotted like module.func)."""
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    names.add(node.func.attr)
                    if isinstance(node.func.value, ast.Name):
                        names.add(f"{node.func.value.id}.{node.func.attr}")
        return names

    def test_session_role_not_imported(self):
        """session_role must not be imported — agent_id guard has no I/O dependencies."""
        tree = self._parse_hook_ast()
        imported = self._collect_import_names(tree)
        assert FORBIDDEN_IMPORT_SESSION_ROLE not in imported, (
            f"Hook must not import '{FORBIDDEN_IMPORT_SESSION_ROLE}'. "
            "Use agent_id guard instead — it requires no imports."
        )

    def test_is_dispatcher_not_called(self):
        """is_dispatcher() must not be called — startup flag is gone at SessionStop."""
        tree = self._parse_hook_ast()
        calls = self._collect_call_names(tree)
        assert FORBIDDEN_CALL_IS_DISPATCHER not in calls, (
            f"Hook must not call '{FORBIDDEN_CALL_IS_DISPATCHER}'. "
            "That function checks the startup-flag file, deleted before SessionStop fires."
        )

    def test_is_dispatcher_session_not_called(self):
        """is_dispatcher_session() must not be called — adds ~10ms subprocess overhead."""
        tree = self._parse_hook_ast()
        calls = self._collect_call_names(tree)
        assert FORBIDDEN_CALL_IS_DISPATCHER_SESSION not in calls, (
            f"Hook must not call '{FORBIDDEN_CALL_IS_DISPATCHER_SESSION}'. "
            "That function uses process-tree walk; agent_id guard is O(1) with no I/O."
        )
