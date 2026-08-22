"""
Unit tests for the `_reconcile_claude_hooks()` wiring added to
hooks/on-fresh-start.py (issue #2249 — the "disappearing agent problem").

## What this covers

`_reconcile_claude_hooks()` must invoke `scripts/reconcile-claude-hooks.py`
via `uv run` on every fresh dispatcher restart, so that hook-wiring drift
between the repo's canonical critical-hook set and a running instance's
`~/.claude/settings.json` self-heals automatically — closing the gap that let
`hooks/auto-register-agent.py` sit unwired long enough for background agents
to leave zero trace on spawn (issue #2249).

Validates:
- Skips cleanly (no crash) when the reconcile script is missing on disk
- Invokes `uv run <path>` for the configured RECONCILE_CLAUDE_HOOKS path
- Never raises on subprocess timeout or unexpected error (fail-open — must
  never block dispatcher startup)
- Exit codes 0/1/2 are all handled without raising
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"


def _make_session_role_stub():
    import types

    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: True
    return stub


def _load_module():
    sys.modules.setdefault("session_role", _make_session_role_stub())
    spec = importlib.util.spec_from_file_location("on_fresh_start_reconcile", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


class TestReconcileClaudeHooksWiring:
    def test_skips_silently_when_script_missing(self, tmp_path: Path) -> None:
        _mod.RECONCILE_CLAUDE_HOOKS = tmp_path / "does-not-exist.py"
        with patch("subprocess.run") as mock_run:
            _mod._reconcile_claude_hooks()
        mock_run.assert_not_called()

    def test_invokes_uv_run_with_configured_script_path(self, tmp_path: Path) -> None:
        script = tmp_path / "reconcile-claude-hooks.py"
        script.write_text("#!/usr/bin/env python3\n")
        _mod.RECONCILE_CLAUDE_HOOKS = script

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            _mod._reconcile_claude_hooks()

        mock_run.assert_called_once()
        called_args = mock_run.call_args[0][0]
        assert called_args == ["uv", "run", str(script)]

    @pytest.mark.parametrize("exit_code", [0, 1, 2])
    def test_never_raises_for_any_known_exit_code(self, tmp_path: Path, exit_code: int) -> None:
        script = tmp_path / "reconcile-claude-hooks.py"
        script.write_text("#!/usr/bin/env python3\n")
        _mod.RECONCILE_CLAUDE_HOOKS = script

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=exit_code, stdout="", stderr="detail"
            )
            _mod._reconcile_claude_hooks()  # must not raise

    def test_never_raises_on_timeout(self, tmp_path: Path) -> None:
        script = tmp_path / "reconcile-claude-hooks.py"
        script.write_text("#!/usr/bin/env python3\n")
        _mod.RECONCILE_CLAUDE_HOOKS = script

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="uv", timeout=15)
            _mod._reconcile_claude_hooks()  # must not raise

    def test_never_raises_on_unexpected_error(self, tmp_path: Path) -> None:
        script = tmp_path / "reconcile-claude-hooks.py"
        script.write_text("#!/usr/bin/env python3\n")
        _mod.RECONCILE_CLAUDE_HOOKS = script

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("boom")
            _mod._reconcile_claude_hooks()  # must not raise
