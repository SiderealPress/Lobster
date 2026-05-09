"""
Unit tests for _is_dispatcher_compact() in on-compact.py (issue #2046).

Root cause: catchup subagents inherit LOBSTER_MAIN_SESSION=1 from the dispatcher
process.  The previous multi-tier logic fell through to the LOBSTER_MAIN_SESSION=1
check for these subagents, causing a cascade of false compact-reminders.

Fix: _is_dispatcher_compact() now checks source='compact' only.
CC sets this field exclusively on post-compact SessionStart hooks.
Catchup subagents are plain SessionStart events without this field, so they
are correctly rejected regardless of any inherited env vars.

The startup flag check (is_dispatcher) handles fresh non-compact dispatcher starts.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-compact.py"

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


def _load_on_compact(*, workspace: Path) -> object:
    """Load on-compact.py with isolated file paths."""
    env = {
        "LOBSTER_WORKSPACE": str(workspace),
        "LOBSTER_STATE_FILE_OVERRIDE": str(workspace / "lobster-state.json"),
        "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE": str(workspace / "compaction-state.json"),
        "LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE": str(workspace / "last-compact.ts"),
        "LOBSTER_OUTBOX_DIR_OVERRIDE": str(workspace / "outbox"),
        "LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE": str(workspace / "last-startup-cause.json"),
    }

    saved = {}
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        _cached_session_role = sys.modules.pop("session_role", None)
        try:
            spec = importlib.util.spec_from_file_location(
                f"on_compact_test_{id(env)}", _HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            inserted = str(_HOOKS_DIR) not in sys.path
            if inserted:
                sys.path.insert(0, str(_HOOKS_DIR))
            try:
                spec.loader.exec_module(mod)
            finally:
                if inserted and str(_HOOKS_DIR) in sys.path:
                    sys.path.remove(str(_HOOKS_DIR))
        finally:
            if _cached_session_role is not None:
                sys.modules["session_role"] = _cached_session_role
            else:
                sys.modules.pop("session_role", None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return mod


# ---------------------------------------------------------------------------
# Core behavior: source='compact' is the sole signal
# ---------------------------------------------------------------------------


class TestIsDispatcherCompact:
    """_is_dispatcher_compact() returns True only for source='compact'."""

    def test_source_compact_returns_true(self, tmp_path):
        """source='compact' → True (post-compact dispatcher session)."""
        mod = _load_on_compact(workspace=tmp_path)

        result = mod._is_dispatcher_compact({"source": "compact"})

        assert result is True, "source='compact' must return True"

    def test_source_start_returns_false(self, tmp_path):
        """source='start' → False (plain session start, not a compaction)."""
        mod = _load_on_compact(workspace=tmp_path)

        result = mod._is_dispatcher_compact({"source": "start"})

        assert result is False, "source='start' must return False"

    def test_no_source_field_returns_false(self, tmp_path):
        """No source field → False (plain SessionStart, e.g. catchup subagent)."""
        mod = _load_on_compact(workspace=tmp_path)

        result = mod._is_dispatcher_compact({"session_id": "any-subagent-id"})

        assert result is False, "Missing source field must return False"

    def test_empty_source_returns_false(self, tmp_path):
        """source='' → False (empty source is not a compact signal)."""
        mod = _load_on_compact(workspace=tmp_path)

        result = mod._is_dispatcher_compact({"source": ""})

        assert result is False, "Empty source must return False"

    def test_catchup_subagent_with_inherited_env_blocked(self, tmp_path):
        """Catchup subagent with LOBSTER_MAIN_SESSION=1 is correctly blocked.

        This is the root cause of #2046: catchup subagents inherit LOBSTER_MAIN_SESSION=1
        from the dispatcher.  The fix must reject them regardless of any inherited env vars
        since they carry no source='compact' signal.
        """
        mod = _load_on_compact(workspace=tmp_path)

        saved = os.environ.get("LOBSTER_MAIN_SESSION")
        os.environ["LOBSTER_MAIN_SESSION"] = "1"
        try:
            result = mod._is_dispatcher_compact({"session_id": "catchup-subagent-0001"})
        finally:
            if saved is None:
                os.environ.pop("LOBSTER_MAIN_SESSION", None)
            else:
                os.environ["LOBSTER_MAIN_SESSION"] = saved

        assert result is False, (
            "Catchup subagent with inherited LOBSTER_MAIN_SESSION=1 and no "
            "source='compact' must be blocked"
        )
