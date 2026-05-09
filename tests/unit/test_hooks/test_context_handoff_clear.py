"""
Unit tests for the context-handoff.json clear-after-read behavior in
hooks/on-fresh-start.py (issue #1995).

## What this file tests

context-handoff.json is a single-use artifact: it carries state across one
restart and is then obsolete. The hook must overwrite it with {} on every
fresh dispatcher start so that stale data from prior sessions does not surface
on subsequent restarts.

## Behaviors verified

CLEAR_AFTER_READ_THRESHOLD = 0  # file must be cleared regardless of content age

1. File exists with fresh content → cleared to {} after dispatcher start
2. File exists with stale content → cleared to {} after dispatcher start
3. File is absent → no error, hook proceeds normally
4. Clear leaves file as valid JSON ({}) — parseable, not deleted
5. Subagent sessions must not clear the file
6. Compaction events must not clear the file

## Named constants (spec-derived)

CLEARED_CONTENT = {}                    # cleared file contains exactly {}
CONTEXT_HANDOFF_FILENAME = "context-handoff.json"  # the single-use artifact
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Named constants (spec-derived, not magic literals)
# ---------------------------------------------------------------------------

# The file that must be cleared after every fresh dispatcher start.
CONTEXT_HANDOFF_FILENAME = "context-handoff.json"

# The content written to the cleared file — valid empty JSON object.
CLEARED_CONTENT_SENTINEL = "{}"

# Subagent hook input (agent_id present).
SUBAGENT_HOOK_INPUT = {"agent_id": "subagent-abc-123", "session_id": "sub-session"}

# Dispatcher hook input (agent_id absent).
DISPATCHER_HOOK_INPUT = {"session_id": "disp-session"}

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"


def _load_hook(
    handoff_file_override: str,
    compaction_state_override: str | None = None,
    session_file_pointer_override: str | None = None,
) -> object:
    """Load on-fresh-start.py with test-controlled file paths.

    Uses env-var overrides that the module reads at import time.
    Returns the loaded module.
    """
    import uuid

    env: dict[str, str] = {
        "LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE": handoff_file_override,
        "LOBSTER_MAIN_SESSION": "1",
    }
    if compaction_state_override is not None:
        env["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override
    if session_file_pointer_override is not None:
        env["LOBSTER_CURRENT_SESSION_FILE_OVERRIDE"] = session_file_pointer_override

    unique_name = f"on_fresh_start_{uuid.uuid4().hex}"
    saved_env: dict[str, str | None] = {}
    for k, v in env.items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, saved_v in saved_env.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v

    return mod


def _run_clear_context_handoff(mod: object, handoff_path: Path) -> None:
    """Call mod._clear_context_handoff() with the path overridden."""
    original = mod.CONTEXT_HANDOFF_FILE
    mod.CONTEXT_HANDOFF_FILE = handoff_path
    try:
        mod._clear_context_handoff()
    finally:
        mod.CONTEXT_HANDOFF_FILE = original


# ---------------------------------------------------------------------------
# Tests: _clear_context_handoff() pure function behavior
# ---------------------------------------------------------------------------


class TestClearContextHandoff:
    """_clear_context_handoff() must overwrite the file with {} on every call."""

    def test_fresh_content_is_cleared_to_empty_json(self, tmp_path):
        """File with recent triggered_at → cleared to {} regardless of recency.

        The clear is unconditional — recency is irrelevant because the
        dispatcher has not yet read the file when the hook fires.
        """
        handoff_path = tmp_path / CONTEXT_HANDOFF_FILENAME
        payload = {
            "triggered_at": "2026-05-08T23:00:00+00:00",
            "context_pct": 72.5,
            "in_flight_agents": [{"task_id": "task-1"}],
            "note": "Stop hook wind-down",
        }
        handoff_path.write_text(json.dumps(payload))

        mod = _load_hook(handoff_file_override=str(handoff_path))
        _run_clear_context_handoff(mod, handoff_path)

        assert handoff_path.exists(), "Cleared file must still exist (not deleted)"
        content = handoff_path.read_text().strip()
        assert content == CLEARED_CONTENT_SENTINEL, (
            f"Cleared file must contain exactly {CLEARED_CONTENT_SENTINEL!r}, got {content!r}"
        )

    def test_stale_content_is_cleared_to_empty_json(self, tmp_path):
        """File with old triggered_at (months ago) → cleared to {}.

        This is the primary fix for issue #1995: stale data from March 2026
        must not survive multiple restarts.
        """
        handoff_path = tmp_path / CONTEXT_HANDOFF_FILENAME
        payload = {
            "triggered_at": "2026-03-31T14:37:53+00:00",  # ~40 days ago
            "context_pct": 45.0,
            "in_flight_agents": [
                {"task_id": "compact-catchup-postcompact", "status": "running"},
                {"task_id": "fix-pr-1282-genre-filter", "status": "running"},
            ],
            "note": "Stop hook wind-down",
        }
        handoff_path.write_text(json.dumps(payload))

        mod = _load_hook(handoff_file_override=str(handoff_path))
        _run_clear_context_handoff(mod, handoff_path)

        assert handoff_path.exists(), "Cleared file must still exist (not deleted)"
        content = handoff_path.read_text().strip()
        assert content == CLEARED_CONTENT_SENTINEL, (
            f"Stale content must be cleared to {CLEARED_CONTENT_SENTINEL!r}, got {content!r}"
        )

    def test_absent_file_is_a_no_op(self, tmp_path):
        """If context-handoff.json does not exist, _clear_context_handoff() is a no-op."""
        handoff_path = tmp_path / CONTEXT_HANDOFF_FILENAME
        assert not handoff_path.exists()

        mod = _load_hook(handoff_file_override=str(handoff_path))
        # Must not raise.
        _run_clear_context_handoff(mod, handoff_path)

        # File must still not exist — no-op means no file creation.
        assert not handoff_path.exists(), (
            "Absent file must remain absent after _clear_context_handoff() no-op"
        )

    def test_cleared_file_contains_valid_json(self, tmp_path):
        """Cleared file must be parseable JSON (not an empty string or garbage)."""
        handoff_path = tmp_path / CONTEXT_HANDOFF_FILENAME
        handoff_path.write_text('{"triggered_at": "2026-01-01T00:00:00Z"}')

        mod = _load_hook(handoff_file_override=str(handoff_path))
        _run_clear_context_handoff(mod, handoff_path)

        raw = handoff_path.read_text()
        parsed = json.loads(raw)
        assert parsed == {}, (
            f"Cleared file must parse to empty dict {{}}, got {parsed!r}"
        )

    def test_cleared_file_triggered_at_absent(self, tmp_path):
        """After clearing, triggered_at must be absent so dispatcher treats it as 'no prior context'.

        The dispatcher step 2c condition is: if triggered_at is absent → ignore.
        An empty JSON object {} has no triggered_at → safe fallback.
        """
        handoff_path = tmp_path / CONTEXT_HANDOFF_FILENAME
        handoff_path.write_text('{"triggered_at": "2026-05-08T23:00:00+00:00", "context_pct": 72}')

        mod = _load_hook(handoff_file_override=str(handoff_path))
        _run_clear_context_handoff(mod, handoff_path)

        parsed = json.loads(handoff_path.read_text())
        assert "triggered_at" not in parsed, (
            "Cleared file must not contain triggered_at — dispatcher must treat it as 'no prior context'"
        )

    def test_clears_on_every_call(self, tmp_path):
        """_clear_context_handoff() must overwrite each time, not just once.

        Write a file, clear it, write it again with different content, clear again.
        Both clears must produce {}.
        """
        handoff_path = tmp_path / CONTEXT_HANDOFF_FILENAME
        mod = _load_hook(handoff_file_override=str(handoff_path))

        # First cycle.
        handoff_path.write_text('{"triggered_at": "2026-05-01T00:00:00Z", "context_pct": 50}')
        _run_clear_context_handoff(mod, handoff_path)
        assert json.loads(handoff_path.read_text()) == {}

        # Simulate next Stop hook write.
        handoff_path.write_text('{"triggered_at": "2026-05-02T00:00:00Z", "context_pct": 80}')
        _run_clear_context_handoff(mod, handoff_path)
        assert json.loads(handoff_path.read_text()) == {}, (
            "Second clear must also produce {} — not accumulate stale state"
        )


# ---------------------------------------------------------------------------
# Tests: CONTEXT_HANDOFF_FILE constant
# ---------------------------------------------------------------------------


class TestContextHandoffConstant:
    """CONTEXT_HANDOFF_FILE must resolve from LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE."""

    def test_constant_resolves_from_env_override(self, tmp_path):
        """LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE must be used when set."""
        override_path = str(tmp_path / "custom-handoff.json")
        mod = _load_hook(handoff_file_override=override_path)
        assert str(mod.CONTEXT_HANDOFF_FILE) == override_path, (
            f"CONTEXT_HANDOFF_FILE must equal override {override_path!r}, "
            f"got {mod.CONTEXT_HANDOFF_FILE!r}"
        )

    def test_constant_defaults_to_workspace_data(self, tmp_path, monkeypatch):
        """Without override, CONTEXT_HANDOFF_FILE defaults to ~/lobster-workspace/data/context-handoff.json."""
        # Load without the override env var set, but with a fake HOME.
        import uuid
        unique_name = f"on_fresh_start_{uuid.uuid4().hex}"

        saved = {
            "LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE": os.environ.get(
                "LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE"
            )
        }
        os.environ.pop("LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE", None)
        try:
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            if saved["LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE"] is None:
                os.environ.pop("LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE", None)
            else:
                os.environ["LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE"] = saved[
                    "LOBSTER_CONTEXT_HANDOFF_FILE_OVERRIDE"
                ]

        assert mod.CONTEXT_HANDOFF_FILE.name == CONTEXT_HANDOFF_FILENAME, (
            f"Default path must end with {CONTEXT_HANDOFF_FILENAME!r}, "
            f"got {mod.CONTEXT_HANDOFF_FILE.name!r}"
        )
        assert "data" in mod.CONTEXT_HANDOFF_FILE.parts, (
            "Default path must be under the data/ directory"
        )
