"""
Unit tests for the fresh-dispatcher-start fallback in inject-bootup-context.py.

Issue #1868 / regression from PR #1891 deploy (issue #1898):
On a fresh restart (new process, MCP server restarted),
inject-bootup-context.py injects the subagent bootup file instead of the
dispatcher bootup file.

Root cause (same as #1768, but not applied to this hook):
- MCP server clears the primary state file (dispatcher-claude-session-id) on startup
- write-dispatcher-session-id.py may skip writing the tertiary file when the
  previous JSONL has a recent mtime
- is_dispatcher() finds no matching file and returns False
- inject-bootup-context.py injects subagent bootup instead of dispatcher bootup

Fix: add _is_fresh_start_dispatcher() as a fallback. Absent primary file +
LOBSTER_MAIN_SESSION=1 → treat as dispatcher. This is safe because:
- subagents are spawned only after the dispatcher has called session_start(),
  which writes the primary file
- compactions are handled by the existing _is_post_compact_dispatcher()
  sentinel fallback (on-compact.py writes the primary file proactively)
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"

if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PatchEnv:
    """Context manager to temporarily set / restore environment variables."""

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


def _load_inject_hook(
    *,
    home: Path,
    workspace: Path,
    lobster_main_session: str | None = "1",
) -> object:
    """Load inject-bootup-context.py in a controlled environment.

    Returns the loaded module. Paths are set at import time via env vars.
    Callers can override module-level path constants after loading.
    """
    import uuid as _uuid

    env: dict = {
        "HOME": str(home),
        "LOBSTER_WORKSPACE": str(workspace),
    }
    if lobster_main_session is None:
        env["LOBSTER_MAIN_SESSION"] = None  # remove from env
    else:
        env["LOBSTER_MAIN_SESSION"] = lobster_main_session

    unique_name = f"inject_bootup_{_uuid.uuid4().hex}"
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _make_bootup_files(claude_dir: Path) -> tuple[Path, Path]:
    """Write minimal dispatcher and subagent bootup stubs."""
    claude_dir.mkdir(parents=True, exist_ok=True)
    dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
    subagent_bootup = claude_dir / "sys.subagent.bootup.md"
    dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
    subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
    return dispatcher_bootup, subagent_bootup


# ---------------------------------------------------------------------------
# Unit tests for _is_fresh_start_dispatcher()
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Issue #1908 removed _is_fresh_start_dispatcher() from inject-bootup-context.py. "
        "Dispatcher detection now uses the launcher-written startup flag (PID file). "
        "These tests are retained for history but are no longer executable."
    )
)
class TestIsFreshStartDispatcher:
    """Unit tests for the _is_fresh_start_dispatcher() helper in inject-bootup-context.py.

    SKIPPED: _is_fresh_start_dispatcher() was removed in issue #1908. Dispatcher
    detection now uses the launcher-written startup flag file (PID-based). The
    UUID/primary-file approach is no longer in use.
    """

    def test_returns_true_when_primary_file_absent_and_main_session(
        self, tmp_path, monkeypatch
    ):
        """Primary file absent + LOBSTER_MAIN_SESSION=1 → fresh dispatcher start."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        absent_primary = data_dir / "dispatcher-claude-session-id"
        # File does NOT exist

        mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)
        import session_role as sr

        with patch.object(sr, "_get_mcp_claude_session_file", return_value=absent_primary):
            result = mod._is_fresh_start_dispatcher()

        assert result is True

    def test_returns_false_when_primary_file_present(self, tmp_path, monkeypatch):
        """Primary file present → dispatcher already called session_start, not a fresh boot."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        primary_file = data_dir / "dispatcher-claude-session-id"
        primary_file.write_text("some-session-uuid-1234")

        mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)
        import session_role as sr

        with patch.object(sr, "_get_mcp_claude_session_file", return_value=primary_file):
            result = mod._is_fresh_start_dispatcher()

        assert result is False

    def test_returns_false_when_main_session_not_set(self, tmp_path, monkeypatch):
        """LOBSTER_MAIN_SESSION not set → not a Lobster-managed session, return False."""
        monkeypatch.delenv("LOBSTER_MAIN_SESSION", raising=False)

        mod = _load_inject_hook(
            home=tmp_path, workspace=tmp_path, lobster_main_session=None
        )
        result = mod._is_fresh_start_dispatcher()

        assert result is False

    def test_returns_false_when_main_session_is_zero(self, tmp_path, monkeypatch):
        """LOBSTER_MAIN_SESSION=0 → return False."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "0")

        mod = _load_inject_hook(
            home=tmp_path, workspace=tmp_path, lobster_main_session="0"
        )
        result = mod._is_fresh_start_dispatcher()

        assert result is False

    def test_returns_false_on_oserror_stat(self, tmp_path, monkeypatch):
        """OSError when checking primary file existence → return False (safe default)."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)

        mock_path = MagicMock(spec=Path)
        mock_path.exists.side_effect = OSError("permission denied")

        import session_role as sr

        with patch.object(sr, "_get_mcp_claude_session_file", return_value=mock_path):
            result = mod._is_fresh_start_dispatcher()

        assert result is False


# ---------------------------------------------------------------------------
# Integration: main() uses fresh-start fallback to inject dispatcher bootup
# ---------------------------------------------------------------------------


class TestMainFreshStartFallback:
    """main() uses the fresh-start fallback to inject dispatcher bootup on fresh restarts."""

    @pytest.mark.skip(
        reason=(
            "Issue #1908 removed _is_fresh_start_dispatcher() and UUID-based detection. "
            "Dispatcher detection now uses the launcher-written startup flag (PID file). "
            "This test scenario is no longer applicable."
        )
    )
    def test_fresh_start_fallback_injects_dispatcher_bootup(
        self, tmp_path, capsys, monkeypatch
    ):
        """Issue #1868 regression test: dispatcher gets dispatcher bootup on fresh restart.

        Simulates the failure mode:
        - write-dispatcher-session-id.py skips updating the tertiary file
          (old JSONL has recent mtime), so tertiary file has the wrong UUID
        - Primary file is absent (MCP server cleared it on startup)
        - is_dispatcher() returns False
        - compact-pending sentinel is absent (fresh restart, not compaction)
        - _is_fresh_start_dispatcher() must detect this and return True
        """
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        # Set up bootup files in tmp structure
        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _ = _make_bootup_files(claude_dir)

        # Primary file absent (MCP cleared it)
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        # No dispatcher-claude-session-id file

        # Tertiary file has wrong (stale) UUID — so is_dispatcher() returns False
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "dispatcher-session-id").write_text("stale-old-uuid-from-dead-session")
        # No compact-pending sentinel (this is a fresh restart, not a compaction)

        hook_input = json.dumps({"session_id": "brand-new-dispatcher-uuid-5678"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": "1",
            }
        ):
            spec = importlib.util.spec_from_file_location(
                "inject_fresh_start_test", _HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            # No compact-pending sentinel
            mod.COMPACT_PENDING_SENTINEL = tmp_path / "no-sentinel"
            # Suppress user config files
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out, (
            "Dispatcher bootup must be injected on fresh start even when "
            "is_dispatcher() returns False and the compact-pending sentinel is absent"
        )
        assert "SUBAGENT BOOTUP" not in captured.out

    def test_subagent_gets_subagent_bootup_when_primary_file_present(
        self, tmp_path, capsys, monkeypatch
    ):
        """When the primary file is present, a non-matching session is a subagent.

        The dispatcher has already called session_start() (writing the primary file),
        so any session whose UUID does not match must be a subagent. The fresh-start
        fallback must NOT fire.
        """
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        claude_dir = tmp_path / "lobster" / ".claude"
        _, subagent_bootup = _make_bootup_files(claude_dir)

        # Primary file present (dispatcher already started and called session_start)
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "dispatcher-claude-session-id").write_text(
            "dispatcher-uuid-already-registered"
        )

        # No sentinel
        (tmp_path / "messages" / "config").mkdir(parents=True, exist_ok=True)

        hook_input = json.dumps({"session_id": "subagent-uuid-different-from-dispatcher"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": "1",
            }
        ):
            spec = importlib.util.spec_from_file_location(
                "inject_subagent_primary_present_test", _HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.COMPACT_PENDING_SENTINEL = tmp_path / "no-sentinel"
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out, (
            "Subagent bootup must be injected when the primary file is present "
            "and the session UUID does not match the dispatcher"
        )
        assert "DISPATCHER BOOTUP" not in captured.out

    def test_fresh_start_fallback_does_not_fire_for_outside_lobster_sessions(
        self, tmp_path, capsys, monkeypatch
    ):
        """LOBSTER_MAIN_SESSION not set → fresh-start fallback inactive, subagent bootup injected."""
        monkeypatch.delenv("LOBSTER_MAIN_SESSION", raising=False)

        claude_dir = tmp_path / "lobster" / ".claude"
        _, subagent_bootup = _make_bootup_files(claude_dir)

        # Primary file absent AND no sentinel — but not a Lobster-managed session
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (tmp_path / "messages" / "config").mkdir(parents=True, exist_ok=True)

        hook_input = json.dumps({"session_id": "some-external-session-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": None,  # unset
            }
        ):
            spec = importlib.util.spec_from_file_location(
                "inject_external_session_test", _HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.COMPACT_PENDING_SENTINEL = tmp_path / "no-sentinel"
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out, (
            "External (non-Lobster) sessions must get subagent bootup regardless "
            "of the primary file state"
        )
        assert "DISPATCHER BOOTUP" not in captured.out

    @pytest.mark.skip(
        reason=(
            "Issue #1908 removed UUID-based primary-file detection from inject-bootup-context.py. "
            "Dispatcher detection now uses the launcher-written startup flag (PID file). "
            "This test scenario is no longer applicable."
        )
    )
    def test_primary_file_match_still_injects_dispatcher_without_fallback(
        self, tmp_path, capsys, monkeypatch
    ):
        """Happy path: primary file matches session UUID → dispatcher bootup, no fallback needed."""
        monkeypatch.setenv("LOBSTER_MAIN_SESSION", "1")

        claude_dir = tmp_path / "lobster" / ".claude"
        dispatcher_bootup, _ = _make_bootup_files(claude_dir)

        dispatcher_uuid = "known-dispatcher-uuid-abc123"

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "dispatcher-claude-session-id").write_text(dispatcher_uuid)

        (tmp_path / "messages" / "config").mkdir(parents=True, exist_ok=True)

        hook_input = json.dumps({"session_id": dispatcher_uuid})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": "1",
            }
        ):
            spec = importlib.util.spec_from_file_location(
                "inject_primary_match_test", _HOOK_PATH
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.COMPACT_PENDING_SENTINEL = tmp_path / "no-sentinel"
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out
        assert "SUBAGENT BOOTUP" not in captured.out
