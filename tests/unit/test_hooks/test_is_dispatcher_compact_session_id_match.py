"""
Unit tests for the exact-session-ID matching fix in _is_dispatcher_compact() (issue #2046).

Root cause: catchup subagents inherit LOBSTER_MAIN_SESSION=1 from the dispatcher
process. When _is_compact_event() returns True via the heartbeat fallback (tier 3),
_is_dispatcher_compact() also returns True for these subagents because LOBSTER_MAIN_SESSION=1
is the only check. This cascades: catchup agents write compact-reminders, which spawn
more catchup agents, which write more compact-reminders.

Fix: _is_dispatcher_compact() now reads the stored dispatcher session ID file
(written by inject-bootup-context.py at fresh dispatcher starts) and does an exact
match against hook_input['session_id'].

Detection tiers (layered, most-to-least reliable):
  1. Startup flag (is_dispatcher): works on fresh starts, not post-compact.
  2. Exact session ID match: hook_input['session_id'] == stored dispatcher session ID.
     Correctly rejects catchup subagents (different session ID).
  3. Post-compact fallback: session ID mismatch + LOBSTER_MAIN_SESSION=1 + explicit
     source/hook_name signal (source='compact' or hook_name='compact').
     Needed because post-compact dispatcher has a NEW session ID (different from stored).
     Only fires when the compact source is explicitly present — NOT for heartbeat fallback.
  4. Backward compat: session ID file absent → LOBSTER_MAIN_SESSION=1 alone.

The critical invariant:
  - Catchup subagents: heartbeat fires but source/hook_name absent → tier 3 is NOT
    triggered → LOBSTER_MAIN_SESSION=1 fallback is NOT used → returns False.
  - Real dispatcher compactions: source='compact' is present → tier 3 fires →
    LOBSTER_MAIN_SESSION=1 used → returns True.

After returning True via tier 3, _is_dispatcher_compact() updates the stored file
with the new session ID so subsequent calls from catchup subagents won't match.
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


def _dispatcher_session_id_file(tmp_path: Path) -> Path:
    """Return the temp-isolated DISPATCHER_SESSION_FILE path for a test."""
    return tmp_path / "messages" / "config" / "dispatcher-session-id"


def _load_on_compact(*, workspace: Path) -> object:
    """Load on-compact.py with isolated file paths.

    Uses LOBSTER_DISPATCHER_SESSION_ID_FILE_OVERRIDE (added in this fix) so that
    DISPATCHER_SESSION_FILE in session_role resolves to a tmp_path subdirectory
    instead of the real ~/messages/config/dispatcher-session-id.

    Removes session_role from sys.modules before loading so that DISPATCHER_SESSION_FILE
    is recomputed from the patched env var, not from the cached import.

    NOTE: LOBSTER_MAIN_SESSION is NOT patched here — it is read by _is_dispatcher_compact()
    at call time, so tests that need a specific value must patch it around the call using
    _PatchEnv({"LOBSTER_MAIN_SESSION": "..."}).  This avoids a subtle bug where the env
    is restored after _load_on_compact() returns but before the test calls the function.
    """
    session_id_file = _dispatcher_session_id_file(workspace)
    env = {
        "LOBSTER_WORKSPACE": str(workspace),
        # Redirect DISPATCHER_SESSION_FILE for test isolation (issue #2046 fix)
        "LOBSTER_DISPATCHER_SESSION_ID_FILE_OVERRIDE": str(session_id_file),
        # Redirect all other file-write side effects to workspace to avoid touching production
        "LOBSTER_STATE_FILE_OVERRIDE": str(workspace / "lobster-state.json"),
        "LOBSTER_COMPACTION_STATE_FILE_OVERRIDE": str(workspace / "compaction-state.json"),
        "LOBSTER_LAST_COMPACT_TS_FILE_OVERRIDE": str(workspace / "last-compact.ts"),
        "LOBSTER_OUTBOX_DIR_OVERRIDE": str(workspace / "outbox"),
        "LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE": str(workspace / "last-startup-cause.json"),
    }

    with _PatchEnv(env):
        # Remove cached session_role so DISPATCHER_SESSION_FILE is recomputed
        # with the patched LOBSTER_DISPATCHER_SESSION_ID_FILE_OVERRIDE env var.
        _cached_session_role = sys.modules.pop("session_role", None)
        try:
            spec = importlib.util.spec_from_file_location(
                f"on_compact_test_{id(env)}", _HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            # Temporarily insert hooks dir so session_role is importable
            inserted = False
            if str(_HOOKS_DIR) not in sys.path:
                sys.path.insert(0, str(_HOOKS_DIR))
                inserted = True
            try:
                spec.loader.exec_module(mod)
            finally:
                if inserted and str(_HOOKS_DIR) in sys.path:
                    sys.path.remove(str(_HOOKS_DIR))
        finally:
            # Restore the cached session_role so other tests aren't affected
            if _cached_session_role is not None:
                sys.modules["session_role"] = _cached_session_role
            else:
                sys.modules.pop("session_role", None)

    return mod


def _write_dispatcher_session_id(tmp_path: Path, session_id: str) -> None:
    """Write a fake dispatcher session ID to the temp-isolated dispatcher-session-id file."""
    f = _dispatcher_session_id_file(tmp_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(session_id)


# ---------------------------------------------------------------------------
# Named constants matching the spec (issue #2046)
# ---------------------------------------------------------------------------

# The number of false compact-reminders observed before this fix
FALSE_COMPACT_CASCADE_THRESHOLD = 3  # 3+ false compactions per genuine one

# Signals that indicate an authoritative compact event from the protocol field
AUTHORITATIVE_COMPACT_SOURCE = "compact"

# A subagent ID pattern (typically distinct from the dispatcher UUID)
CATCHUP_SUBAGENT_ID = "catchup-subagent-session-0001"

# The dispatcher's real session ID (stored at startup)
DISPATCHER_SESSION_ID = "dispatcher-real-session-uuid-abcd"

# The dispatcher's NEW session ID after compaction (CC assigns a new ID)
DISPATCHER_POST_COMPACT_SESSION_ID = "dispatcher-post-compact-uuid-efgh"


# ---------------------------------------------------------------------------
# Tier 2: Exact session ID match
# ---------------------------------------------------------------------------


class TestExactSessionIdMatch:
    """_is_dispatcher_compact() returns True when session IDs match exactly."""

    def test_exact_match_returns_true(self, tmp_path):
        """Stored session ID == hook_input session_id → True (dispatcher confirmed)."""
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        result = mod._is_dispatcher_compact({"session_id": DISPATCHER_SESSION_ID})

        assert result is True, (
            "Exact match between stored and current session ID must return True"
        )

    def test_different_session_id_returns_false_without_compact_source(self, tmp_path):
        """Stored session ID != current AND no authoritative compact source → False.

        This is the subagent case: the subagent has a different session ID, and the
        compact source is absent (detected via heartbeat fallback only). Without the
        explicit source/hook_name signal, we do not fall through to LOBSTER_MAIN_SESSION.
        """
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        # No source, no hook_name — only heartbeat fallback would have triggered this.
        # LOBSTER_MAIN_SESSION=1 is explicitly patched at call time to confirm the fix
        # blocks the cascade even when the env var is inherited.
        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            result = mod._is_dispatcher_compact({"session_id": CATCHUP_SUBAGENT_ID})

        assert result is False, (
            "Session ID mismatch + no explicit compact source must return False "
            "(subagent detected, not a real dispatcher compaction)"
        )

    def test_catchup_subagent_with_lobster_main_session_blocked(self, tmp_path):
        """Catchup subagent with LOBSTER_MAIN_SESSION=1 is correctly blocked.

        This is the root cause of #2046: catchup subagents inherit LOBSTER_MAIN_SESSION=1
        from the dispatcher. Without the session ID check, they pass _is_dispatcher_compact()
        and write false compact-reminders.
        """
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        # Catchup subagent session: no source/hook_name field (heartbeat fallback only).
        # LOBSTER_MAIN_SESSION=1 is patched at call time — this is the inherited value.
        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            result = mod._is_dispatcher_compact({"session_id": CATCHUP_SUBAGENT_ID})

        assert result is False, (
            "Catchup subagent with inherited LOBSTER_MAIN_SESSION=1 must be blocked "
            "when session ID does not match the stored dispatcher session ID"
        )


# ---------------------------------------------------------------------------
# Tier 3: Post-compact dispatcher fallback
# ---------------------------------------------------------------------------


class TestPostCompactDispatcherFallback:
    """_is_dispatcher_compact() returns True for real dispatcher compactions
    even when the session ID is new (different from stored).

    Real compactions have source='compact' or hook_name='compact' in the hook input.
    Catchup subagents do not — they only trigger via heartbeat fallback.
    """

    def test_post_compact_new_session_id_with_source_compact_returns_true(self, tmp_path):
        """Post-compact session: new session ID + source='compact' + LOBSTER_MAIN_SESSION=1 → True.

        The dispatcher's post-compact session has a NEW session ID (different from stored),
        but source='compact' is the authoritative signal that this is a real compaction.
        """
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            result = mod._is_dispatcher_compact({
                "session_id": DISPATCHER_POST_COMPACT_SESSION_ID,
                "source": AUTHORITATIVE_COMPACT_SOURCE,
            })

        assert result is True, (
            "Post-compact dispatcher with source='compact' must return True "
            "even when session ID differs from stored"
        )

    def test_post_compact_new_session_id_with_hook_name_compact_returns_true(self, tmp_path):
        """Post-compact session: new session ID + hook_name='compact' → True.

        hook_name='compact' is the fallback signal (older CC versions) that still
        indicates an authoritative compact event.
        """
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            result = mod._is_dispatcher_compact({
                "session_id": DISPATCHER_POST_COMPACT_SESSION_ID,
                "hook_name": AUTHORITATIVE_COMPACT_SOURCE,
            })

        assert result is True, (
            "Post-compact dispatcher with hook_name='compact' must return True "
            "even when session ID differs from stored"
        )

    def test_post_compact_updates_stored_session_id(self, tmp_path):
        """After a post-compact dispatcher is confirmed, the stored session ID is updated.

        This ensures catchup subagents spawned AFTER this compaction won't match the
        new dispatcher session ID (they have their own distinct session IDs).
        """
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            mod._is_dispatcher_compact({
                "session_id": DISPATCHER_POST_COMPACT_SESSION_ID,
                "source": AUTHORITATIVE_COMPACT_SOURCE,
            })

        # The stored session ID file must now contain the new post-compact session ID
        stored = (tmp_path / "messages" / "config" / "dispatcher-session-id").read_text().strip()
        assert stored == DISPATCHER_POST_COMPACT_SESSION_ID, (
            "After confirming a post-compact dispatcher, the stored session ID "
            "must be updated to the new session ID"
        )

    def test_catchup_subagent_without_main_session_env_returns_false(self, tmp_path):
        """Even with source='compact', a different session AND LOBSTER_MAIN_SESSION != '1' → False.

        If LOBSTER_MAIN_SESSION is not set to '1', the post-compact fallback doesn't apply.
        """
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        # Explicitly patch LOBSTER_MAIN_SESSION to "0" at call time (not just load time)
        # to verify the LOBSTER_MAIN_SESSION check is evaluated at call time.
        with _PatchEnv({"LOBSTER_MAIN_SESSION": "0"}):
            result = mod._is_dispatcher_compact({
                "session_id": CATCHUP_SUBAGENT_ID,
                "source": AUTHORITATIVE_COMPACT_SOURCE,
            })

        assert result is False, (
            "Session ID mismatch + LOBSTER_MAIN_SESSION != '1' → must return False"
        )


# ---------------------------------------------------------------------------
# Tier 4: Backward compat (no session ID file)
# ---------------------------------------------------------------------------


class TestBackwardCompatNoSessionIdFile:
    """When no dispatcher session ID file exists, fall back to LOBSTER_MAIN_SESSION=1."""

    def test_no_session_id_file_lobster_main_session_1_returns_true(self, tmp_path):
        """No session ID file + LOBSTER_MAIN_SESSION=1 → True (backward compat).

        This handles the case where the system hasn't been updated yet (fresh install
        without inject-bootup-context.py having run once to write the file).
        """
        # Do NOT write the dispatcher-session-id file
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            result = mod._is_dispatcher_compact({"session_id": "any-session-id"})

        assert result is True, (
            "No session ID file + LOBSTER_MAIN_SESSION=1 must return True "
            "(backward compat: pre-fix installs don't have the file yet)"
        )

    def test_no_session_id_file_lobster_main_session_0_returns_false(self, tmp_path):
        """No session ID file + LOBSTER_MAIN_SESSION != '1' → False."""
        mod = _load_on_compact(workspace=tmp_path)

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "0"}):
            result = mod._is_dispatcher_compact({"session_id": "any-session-id"})

        assert result is False, (
            "No session ID file + LOBSTER_MAIN_SESSION != '1' must return False"
        )


# ---------------------------------------------------------------------------
# Integration: full cascade prevention
# ---------------------------------------------------------------------------


class TestCascadePrevention:
    """End-to-end tests verifying the cascade scenario from issue #2046 is fixed."""

    def test_catchup_subagent_after_real_compaction_returns_false(self, tmp_path):
        """After a real compaction, catchup subagents are correctly rejected.

        Scenario:
        1. Dispatcher starts with session ID A (written to file by inject-bootup-context.py)
        2. Dispatcher compacts → new session B, source='compact' → returns True
           → file updated to session B
        3. Catchup subagent starts with session C, no source/hook_name → returns False

        Step 3 is the key fix: without session ID matching, step 3 returned True
        because LOBSTER_MAIN_SESSION=1 was the only check.
        """
        # Step 1: initial dispatcher session written at startup
        _write_dispatcher_session_id(tmp_path, DISPATCHER_SESSION_ID)

        mod = _load_on_compact(workspace=tmp_path)

        # Step 2: real dispatcher compaction (LOBSTER_MAIN_SESSION=1 patched at call time)
        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            result_real = mod._is_dispatcher_compact({
                "session_id": DISPATCHER_POST_COMPACT_SESSION_ID,
                "source": AUTHORITATIVE_COMPACT_SOURCE,
            })
        assert result_real is True, "Real dispatcher compaction must return True"

        # Verify file was updated
        stored = (tmp_path / "messages" / "config" / "dispatcher-session-id").read_text().strip()
        assert stored == DISPATCHER_POST_COMPACT_SESSION_ID

        # Step 3: catchup subagent (heartbeat fallback only, no source field).
        # LOBSTER_MAIN_SESSION=1 is still inherited — the fix must reject it anyway.
        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            result_catchup = mod._is_dispatcher_compact({"session_id": CATCHUP_SUBAGENT_ID})
        assert result_catchup is False, (
            "Catchup subagent after real compaction must return False — "
            "this prevents the false compact-reminder cascade"
        )

    def test_multiple_catchup_subagents_all_blocked(self, tmp_path):
        """All catchup subagents in a cascade are blocked, not just the first."""
        # Multiple subagents inherit LOBSTER_MAIN_SESSION=1 and have the heartbeat fire
        _write_dispatcher_session_id(tmp_path, DISPATCHER_POST_COMPACT_SESSION_ID)
        mod = _load_on_compact(workspace=tmp_path)

        # Simulate FALSE_COMPACT_CASCADE_THRESHOLD catchup subagents from issue #2046
        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1"}):
            for i in range(FALSE_COMPACT_CASCADE_THRESHOLD + 1):
                subagent_id = f"catchup-subagent-{i:04d}-session-id"
                result = mod._is_dispatcher_compact({"session_id": subagent_id})
                assert result is False, (
                    f"Catchup subagent {i} must be blocked (session ID mismatch, "
                    f"no authoritative compact source)"
                )
