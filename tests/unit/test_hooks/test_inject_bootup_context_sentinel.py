"""
Unit tests for Option 3 of issue #1375: the sentinel-based fallback in
inject-bootup-context.py.

When the compact-pending sentinel file exists AND LOBSTER_MAIN_SESSION=1,
inject-bootup-context.py must inject dispatcher bootup context regardless of
what session_role.is_dispatcher() returns.

This covers the case where Option 1 (writing the UUID in on-compact.py)
hasn't propagated yet or fails silently, providing defense-in-depth for the
post-compact window.

Tests focus on the _is_post_compact_dispatcher() helper and the integration
path in main() that uses it as a fallback.
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


def _load_inject_hook(*, home: Path, workspace: Path) -> object:
    """Load inject-bootup-context.py in a controlled environment."""
    env = {
        "HOME": str(home),
        "LOBSTER_WORKSPACE": str(workspace),
    }
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location("inject_bootup", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Tests for _is_post_compact_dispatcher()
# ---------------------------------------------------------------------------


class TestIsPostCompactDispatcher:
    """Unit tests for the _is_post_compact_dispatcher() helper."""

    def test_returns_true_when_sentinel_exists_and_main_session(self, tmp_path):
        """Sentinel present + LOBSTER_MAIN_SESSION=1 → True."""
        # Create sentinel.
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        sentinel = config_dir / "compact-pending"
        sentinel.touch()

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1", "HOME": str(tmp_path)}):
            mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)
            # Override the sentinel path to point at our tmp sentinel.
            mod.COMPACT_PENDING_SENTINEL = sentinel
            result = mod._is_post_compact_dispatcher()

        assert result is True

    def test_returns_false_when_no_sentinel(self, tmp_path):
        """Sentinel absent → False even if LOBSTER_MAIN_SESSION=1."""
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        sentinel = config_dir / "compact-pending"
        # sentinel does NOT exist

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "1", "HOME": str(tmp_path)}):
            mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)
            mod.COMPACT_PENDING_SENTINEL = sentinel
            result = mod._is_post_compact_dispatcher()

        assert result is False

    def test_returns_false_when_lobster_main_session_not_set(self, tmp_path):
        """LOBSTER_MAIN_SESSION absent → False even if sentinel exists."""
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        sentinel = config_dir / "compact-pending"
        sentinel.touch()

        with _PatchEnv({"LOBSTER_MAIN_SESSION": None, "HOME": str(tmp_path)}):
            mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)
            mod.COMPACT_PENDING_SENTINEL = sentinel
            result = mod._is_post_compact_dispatcher()

        assert result is False

    def test_returns_false_when_lobster_main_session_is_zero(self, tmp_path):
        """LOBSTER_MAIN_SESSION=0 → False even if sentinel exists."""
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        sentinel = config_dir / "compact-pending"
        sentinel.touch()

        with _PatchEnv({"LOBSTER_MAIN_SESSION": "0", "HOME": str(tmp_path)}):
            mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)
            mod.COMPACT_PENDING_SENTINEL = sentinel
            result = mod._is_post_compact_dispatcher()

        assert result is False

    def test_returns_false_when_both_absent(self, tmp_path):
        """Neither sentinel nor LOBSTER_MAIN_SESSION=1 → False."""
        sentinel = tmp_path / "no-sentinel"

        with _PatchEnv({"LOBSTER_MAIN_SESSION": None, "HOME": str(tmp_path)}):
            mod = _load_inject_hook(home=tmp_path, workspace=tmp_path)
            mod.COMPACT_PENDING_SENTINEL = sentinel
            result = mod._is_post_compact_dispatcher()

        assert result is False


# ---------------------------------------------------------------------------
# Integration: main() uses sentinel fallback to inject dispatcher bootup
# ---------------------------------------------------------------------------


class TestMainSentinelFallback:
    """main() in inject-bootup-context.py uses the sentinel as a fallback
    when is_dispatcher() returns False but the post-compact sentinel is present.
    """

    def _make_bootup_files(self, claude_dir: Path) -> tuple[Path, Path]:
        """Write minimal dispatcher and subagent bootup stubs."""
        dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
        subagent_bootup = claude_dir / "sys.subagent.bootup.md"
        dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
        subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
        return dispatcher_bootup, subagent_bootup

    def test_sentinel_fallback_injects_dispatcher_bootup(self, tmp_path, capsys):
        """When is_dispatcher()=False but sentinel+LOBSTER_MAIN_SESSION=1,
        dispatcher bootup must be injected (not subagent bootup).
        """
        # Set up file structure.
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        dispatcher_bootup, _ = self._make_bootup_files(claude_dir)

        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        sentinel = config_dir / "compact-pending"
        sentinel.touch()

        # No dispatcher session ID files → is_dispatcher() will return False.
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        hook_input = json.dumps({"session_id": "unknown-post-compact-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": "1",
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_main_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Override paths to use our tmp structure.
            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            mod.COMPACT_PENDING_SENTINEL = sentinel
            # Point user config to a non-existent dir so extra files aren't injected.
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "DISPATCHER BOOTUP" in captured.out, (
            "Dispatcher bootup should have been injected via sentinel fallback"
        )
        assert "SUBAGENT BOOTUP" not in captured.out

    def test_no_sentinel_normal_subagent_gets_subagent_bootup(self, tmp_path, capsys):
        """Without sentinel, an unrecognised session gets subagent bootup (normal path)."""
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        _, subagent_bootup = self._make_bootup_files(claude_dir)

        # No sentinel, no dispatcher session files.
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        hook_input = json.dumps({"session_id": "random-subagent-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": "1",
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_sub_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Override paths.
            mod.SUBAGENT_BOOTUP = subagent_bootup
            sentinel_path = tmp_path / "messages" / "config" / "compact-pending"
            mod.COMPACT_PENDING_SENTINEL = sentinel_path  # does not exist
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out, (
            "Subagent bootup should be injected for unrecognised session without sentinel"
        )
        assert "DISPATCHER BOOTUP" not in captured.out

    def test_sentinel_with_lobster_main_session_zero_gets_subagent_bootup(
        self, tmp_path, capsys
    ):
        """Sentinel present but LOBSTER_MAIN_SESSION != '1' → subagent bootup, not dispatcher."""
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        _, subagent_bootup = self._make_bootup_files(claude_dir)

        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        sentinel = config_dir / "compact-pending"
        sentinel.touch()

        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        hook_input = json.dumps({"session_id": "outside-session-uuid"})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": "0",  # NOT the main session
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_outside_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.SUBAGENT_BOOTUP = subagent_bootup
            mod.COMPACT_PENDING_SENTINEL = sentinel
            mod.USER_CONFIG_DIR = tmp_path / "no-user-config"
            mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
            mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
            mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

            with patch("sys.stdin", io.StringIO(hook_input)):
                with pytest.raises(SystemExit):
                    mod.main()

        captured = capsys.readouterr()
        assert "SUBAGENT BOOTUP" in captured.out
        assert "DISPATCHER BOOTUP" not in captured.out

    def test_primary_file_match_still_works_without_sentinel(self, tmp_path, capsys):
        """When is_dispatcher() returns True via primary file, no sentinel needed."""
        claude_dir = tmp_path / "lobster" / ".claude"
        claude_dir.mkdir(parents=True)
        dispatcher_bootup, _ = self._make_bootup_files(claude_dir)

        # Write dispatcher UUID to primary file.
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        dispatcher_uuid = "known-dispatcher-uuid-abc"
        (data_dir / "dispatcher-claude-session-id").write_text(dispatcher_uuid)

        # No sentinel.
        (tmp_path / "messages" / "config").mkdir(parents=True, exist_ok=True)

        hook_input = json.dumps({"session_id": dispatcher_uuid})

        with _PatchEnv(
            {
                "HOME": str(tmp_path),
                "LOBSTER_WORKSPACE": str(tmp_path),
                "LOBSTER_MAIN_SESSION": "1",
            }
        ):
            spec = importlib.util.spec_from_file_location("inject_primary_test", _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mod.DISPATCHER_BOOTUP = dispatcher_bootup
            sentinel_path = tmp_path / "messages" / "config" / "compact-pending"
            mod.COMPACT_PENDING_SENTINEL = sentinel_path  # does not exist
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
