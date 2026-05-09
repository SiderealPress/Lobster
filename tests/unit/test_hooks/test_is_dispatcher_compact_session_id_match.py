"""
Unit tests for _is_dispatcher_compact() in on-compact.py (issue #2046).

Root cause: catchup subagents inherit LOBSTER_MAIN_SESSION=1 from the dispatcher
process.  The previous multi-tier logic fell through to the LOBSTER_MAIN_SESSION=1
check for these subagents, causing a cascade of false compact-reminders.

Fix: _is_dispatcher_compact() uses source='compact' as the primary signal, with
an additional session ID tier for belt-and-suspenders rejection of subagent
compactions that carry source='compact'.

Session ID tier:
  CC preserves the CC session UUID across compactions (same UUID before and after
  compact).  inject-bootup-context.py writes the dispatcher's session UUID to
  DISPATCHER_SESSION_FILE at fresh starts.  When source='compact' fires:
    - stored_id == current session_id → confirmed dispatcher compact
    - stored_id != current session_id → subagent compact → rejected
    - No stored_id (or no current session_id) → fail-open, source='compact' wins

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


class _PatchEnv:
    """Context manager to temporarily set/unset environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved: dict = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _session_id_file_path(workspace: Path) -> Path:
    """Return the temp-isolated dispatcher session ID file path."""
    return workspace / "messages" / "config" / "dispatcher-session-id"


def _make_env(workspace: Path) -> dict:
    """Build the env dict for test isolation, including DISPATCHER_SESSION_ID override."""
    return {
        "LOBSTER_WORKSPACE": str(workspace),
        "LOBSTER_DISPATCHER_SESSION_ID_FILE_OVERRIDE": str(_session_id_file_path(workspace)),
        "LOBSTER_STATE_FILE_OVERRIDE": str(workspace / "lobster-state.json"),
        "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE": str(workspace / "compaction-state.json"),
        "LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE": str(workspace / "last-compact.ts"),
        "LOBSTER_OUTBOX_DIR_OVERRIDE": str(workspace / "outbox"),
        "LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE": str(workspace / "last-startup-cause.json"),
    }


def _load_on_compact(*, workspace: Path) -> object:
    """Load on-compact.py with isolated file paths.

    Uses LOBSTER_DISPATCHER_SESSION_ID_FILE_OVERRIDE so that DISPATCHER_SESSION_FILE
    in session_role resolves to a tmp_path subdirectory instead of the real
    ~/messages/config/dispatcher-session-id.  This prevents tests from reading
    or writing the production session ID file.

    NOTE: env vars are restored AFTER loading.  To call functions on the returned
    module with env vars still active, use _call_with_env(mod, workspace, ...) or
    wrap the call in _PatchEnv(_make_env(workspace)).
    """
    env = _make_env(workspace)

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


def _write_dispatcher_session_id(workspace: Path, session_id: str) -> None:
    """Write a fake dispatcher session ID to the temp-isolated dispatcher-session-id file."""
    f = _session_id_file_path(workspace)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(session_id)


# ---------------------------------------------------------------------------
# Core behavior: source='compact' gating
# ---------------------------------------------------------------------------


class TestIsDispatcherCompact:
    """_is_dispatcher_compact() basic source='compact' gating (no stored session ID)."""

    def test_source_compact_no_stored_id_returns_true(self, tmp_path):
        """source='compact' with no stored session ID → True (fail-open backward compat)."""
        mod = _load_on_compact(workspace=tmp_path)

        result = mod._is_dispatcher_compact({"source": "compact"})

        assert result is True, "source='compact' with no stored ID must return True (fail-open)"

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


# ---------------------------------------------------------------------------
# Session ID tier: stored ID present
# ---------------------------------------------------------------------------


class TestIsDispatcherCompactSessionIdTier:
    """Session ID tier: stored dispatcher session ID present.

    These tests use _PatchEnv to keep LOBSTER_DISPATCHER_SESSION_ID_FILE_OVERRIDE
    active during the function call, since _read_dispatcher_session_id() resolves
    the file path dynamically at call time.
    """

    def test_matching_session_id_returns_true(self, tmp_path):
        """source='compact' + session_id matches stored dispatcher ID → True.

        This is the normal post-compact dispatcher path: CC preserves the
        CC session UUID across compactions, so the stored ID (written at fresh
        start) matches the post-compact session's session_id.
        """
        dispatcher_uuid = "3cf478f7-fbeb-4a84-8a6c-d5fd90da7a3f"
        _write_dispatcher_session_id(tmp_path, dispatcher_uuid)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv(_make_env(tmp_path)):
            result = mod._is_dispatcher_compact({
                "source": "compact",
                "session_id": dispatcher_uuid,
            })

        assert result is True, "Matching session ID + source='compact' must return True"

    def test_mismatching_session_id_returns_false(self, tmp_path):
        """source='compact' + session_id does NOT match stored dispatcher ID → False.

        A subagent that genuinely compacts would have its own unique session ID,
        different from the stored dispatcher session ID.  This tier rejects it.
        """
        dispatcher_uuid = "3cf478f7-fbeb-4a84-8a6c-d5fd90da7a3f"
        subagent_uuid = "aabbccdd-1122-3344-5566-778899001122"
        _write_dispatcher_session_id(tmp_path, dispatcher_uuid)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv(_make_env(tmp_path)):
            result = mod._is_dispatcher_compact({
                "source": "compact",
                "session_id": subagent_uuid,
            })

        assert result is False, (
            "source='compact' with mismatching session ID must return False "
            "(subagent compact, not dispatcher)"
        )

    def test_no_session_id_in_payload_failopen(self, tmp_path):
        """source='compact' + stored ID present but no session_id in payload → True (fail-open).

        If the hook payload lacks a session_id field, we cannot perform the ID check.
        Fall back to source='compact' as the authoritative signal.
        """
        dispatcher_uuid = "3cf478f7-fbeb-4a84-8a6c-d5fd90da7a3f"
        _write_dispatcher_session_id(tmp_path, dispatcher_uuid)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv(_make_env(tmp_path)):
            result = mod._is_dispatcher_compact({"source": "compact"})

        assert result is True, (
            "source='compact' with no session_id in payload must return True (fail-open)"
        )

    def test_empty_session_id_in_payload_failopen(self, tmp_path):
        """source='compact' + stored ID present but empty session_id → True (fail-open)."""
        dispatcher_uuid = "3cf478f7-fbeb-4a84-8a6c-d5fd90da7a3f"
        _write_dispatcher_session_id(tmp_path, dispatcher_uuid)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv(_make_env(tmp_path)):
            result = mod._is_dispatcher_compact({"source": "compact", "session_id": ""})

        assert result is True, (
            "source='compact' with empty session_id must return True (fail-open)"
        )

    def test_cascade_subagent_blocked_when_dispatcher_id_stored(self, tmp_path):
        """A subagent compact is blocked when the dispatcher session ID is stored.

        Scenario (the cascade bug scenario from #2046):
          1. Dispatcher starts fresh → inject-bootup-context.py writes dispatcher UUID.
          2. Dispatcher compacts (source='compact', same UUID) → detected correctly.
          3. CC spawns a catchup subagent (no source field) → blocked by _is_compact_event.
          4. Catchup subagent grows large and compacts → source='compact' fires, but
             the subagent's session UUID does NOT match the stored dispatcher UUID → rejected.

        This test covers step 4 — the belt-and-suspenders rejection of a genuinely
        compacting subagent that has source='compact' from CC.
        """
        dispatcher_uuid = "3cf478f7-fbeb-4a84-8a6c-d5fd90da7a3f"
        subagent_uuid = "catchup-subagent-compact-uuid-0001"
        _write_dispatcher_session_id(tmp_path, dispatcher_uuid)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv(_make_env(tmp_path)):
            result = mod._is_dispatcher_compact({
                "source": "compact",
                "session_id": subagent_uuid,
            })

        assert result is False, (
            "Subagent compact (source='compact' + mismatched session ID) must be blocked "
            "even when LOBSTER_MAIN_SESSION=1 is set"
        )
