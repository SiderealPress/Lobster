"""
Tests for scripts/prune-pr-worktrees.py

Covers:
- TimeoutExpired in the scan loop does NOT abort the full run
- Fallback rm -rf is gated on absence from `git worktree list --porcelain`
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test.  The script lives in scripts/ which is not a
# Python package, so we add it to sys.path manually.
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture(autouse=True)
def _add_scripts_to_path(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))


import importlib

@pytest.fixture()
def prune():
    """Return a freshly-imported prune_pr_worktrees module."""
    # Use importlib so we can re-import cleanly per test.
    import importlib
    mod = importlib.import_module("prune-pr-worktrees".replace("-", "_"))
    return mod


# ---------------------------------------------------------------------------
# Helper — importlib can't import a module whose name contains a hyphen via
# normal attribute access, so we do it once at module level.
# ---------------------------------------------------------------------------
import importlib.util, types

_spec = importlib.util.spec_from_file_location(
    "prune_pr_worktrees",
    SCRIPTS_DIR / "prune-pr-worktrees.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

run = _mod.run
is_worktree_registered = _mod.is_worktree_registered
remove_worktree = _mod.remove_worktree


# ===========================================================================
# Tests for TimeoutExpired handling in run()
# ===========================================================================


class TestRunTimeoutExpired:
    """run() must catch TimeoutExpired and return (1, '', 'timeout')."""

    def test_timeout_returns_error_tuple(self, tmp_path):
        """When the subprocess hangs, run() returns (1, '', 'timeout')."""
        # Patch Popen so that communicate() raises TimeoutExpired the first time
        # (triggering the exception handler), then succeeds when called again
        # after kill() to reap the process.
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["git"], timeout=30),
            ("", ""),  # second call after kill() to reap
        ]
        with patch("subprocess.Popen", return_value=mock_proc):
            rc, stdout, stderr = run(["git", "branch"], timeout=30)
        assert rc == 1
        assert stdout == ""
        assert stderr == "timeout"

    def test_timeout_kills_process(self, tmp_path):
        """When TimeoutExpired fires, the child process is killed and reaped."""
        mock_proc = MagicMock()
        # First communicate() raises; second (after kill) succeeds.
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["git"], timeout=30),
            ("", ""),
        ]
        with patch("subprocess.Popen", return_value=mock_proc):
            run(["git", "branch"], timeout=30)
        mock_proc.kill.assert_called_once()
        # communicate() called twice: once (raises), once after kill
        assert mock_proc.communicate.call_count == 2

    def test_timeout_does_not_propagate(self, tmp_path):
        """TimeoutExpired must not escape run() — callers see a normal tuple."""
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["gh"], timeout=30),
            ("", ""),  # second call after kill() to reap
        ]
        with patch("subprocess.Popen", return_value=mock_proc):
            result = run(["gh", "pr", "list"])
        # If the exception had escaped, this line would not be reached.
        assert isinstance(result, tuple) and len(result) == 3


# ===========================================================================
# Tests for is_worktree_registered()
# ===========================================================================


class TestIsWorktreeRegistered:
    """is_worktree_registered() parses `git worktree list --porcelain` output."""

    def _porcelain_output(self, paths: list[str]) -> str:
        """Build fake --porcelain output for the given worktree paths."""
        blocks = []
        for p in paths:
            blocks.append(f"worktree {p}\nHEAD abc123\nbranch refs/heads/main\n")
        return "\n".join(blocks)

    def test_returns_true_when_path_listed(self, tmp_path):
        wt = tmp_path / "my-worktree"
        wt.mkdir()
        main_repo = tmp_path / "main"
        porcelain = self._porcelain_output([str(wt.resolve())])
        with patch.object(_mod, "run", return_value=(0, porcelain, "")):
            assert is_worktree_registered(main_repo, wt) is True

    def test_returns_false_when_path_absent(self, tmp_path):
        wt = tmp_path / "my-worktree"
        wt.mkdir()
        main_repo = tmp_path / "main"
        other = str(tmp_path / "other-worktree")
        porcelain = self._porcelain_output([other])
        with patch.object(_mod, "run", return_value=(0, porcelain, "")):
            assert is_worktree_registered(main_repo, wt) is False

    def test_returns_true_conservatively_when_git_fails(self, tmp_path):
        """If git worktree list fails, assume registered (safe default)."""
        wt = tmp_path / "my-worktree"
        main_repo = tmp_path / "main"
        with patch.object(_mod, "run", return_value=(1, "", "fatal: not a git repo")):
            assert is_worktree_registered(main_repo, wt) is True


# ===========================================================================
# Tests for remove_worktree() fallback gating
# ===========================================================================


class TestRemoveWorktreeFallbackGating:
    """Fallback rm -rf must only run when the worktree is absent from git list."""

    def test_no_rmtree_when_still_registered(self, tmp_path):
        """If git worktree remove fails AND the worktree is still registered,
        shutil.rmtree must NOT be called."""
        wt = tmp_path / "stale-wt"
        wt.mkdir()
        main_repo = tmp_path / "main"

        # git worktree remove fails; git worktree list shows it as registered
        def fake_run(cmd, cwd=None, timeout=30):
            if "remove" in cmd:
                return 1, "", "error: not a worktree"
            if "list" in cmd and "--porcelain" in cmd:
                return 0, f"worktree {wt.resolve()}\nHEAD abc\nbranch refs/heads/x\n", ""
            return 0, "", ""

        with patch.object(_mod, "run", side_effect=fake_run), \
             patch("shutil.rmtree") as mock_rm:
            result = remove_worktree(main_repo, wt, dry_run=False)

        assert result is False, "Should return False — refused to rm -rf a registered worktree"
        mock_rm.assert_not_called()

    def test_rmtree_allowed_when_not_registered(self, tmp_path):
        """If git worktree remove fails AND the worktree is absent from list,
        shutil.rmtree IS allowed (path-safety permitting)."""
        # Put the worktree under DEFAULT_PROJECTS_DIR so the path safety check passes
        import importlib
        projects_dir = _mod.DEFAULT_PROJECTS_DIR.resolve()
        wt = projects_dir / "orphan-wt"
        wt.mkdir(parents=True, exist_ok=True)
        main_repo = tmp_path / "main"

        # git worktree remove fails; git worktree list does NOT show this path
        def fake_run(cmd, cwd=None, timeout=30):
            if "remove" in cmd:
                return 1, "", "error: not a worktree"
            if "list" in cmd and "--porcelain" in cmd:
                # List shows a different path — target is absent
                return 0, "worktree /some/other/path\nHEAD abc\nbranch refs/heads/x\n", ""
            # prune or other git commands
            return 0, "", ""

        with patch.object(_mod, "run", side_effect=fake_run), \
             patch("shutil.rmtree") as mock_rm:
            result = remove_worktree(main_repo, wt, dry_run=False)

        assert result is True
        mock_rm.assert_called_once_with(str(wt))

        # cleanup
        import shutil
        if wt.exists():
            shutil.rmtree(str(wt))

    def test_git_worktree_remove_success_skips_fallback(self, tmp_path):
        """When git worktree remove succeeds, shutil.rmtree is never called."""
        wt = tmp_path / "clean-wt"
        wt.mkdir()
        main_repo = tmp_path / "main"

        def fake_run(cmd, cwd=None, timeout=30):
            if "remove" in cmd:
                return 0, "", ""
            return 0, "", ""

        with patch.object(_mod, "run", side_effect=fake_run), \
             patch("shutil.rmtree") as mock_rm:
            result = remove_worktree(main_repo, wt, dry_run=False)

        assert result is True
        mock_rm.assert_not_called()


# ===========================================================================
# Integration-style test: scan loop survives a TimeoutExpired mid-scan
# ===========================================================================


class TestScanLoopSurvivesTimeout:
    """The main scan loop must not abort when a sub-command times out."""

    def test_scan_continues_after_timeout(self, tmp_path, capsys):
        """If get_branch() (which calls run()) returns a timeout, the loop
        continues and the summary line is still emitted."""
        # Build two fake worktree dirs in a clean scan root
        scan_root = tmp_path / "scan_root"
        scan_root.mkdir()
        for name in ("wt-a", "wt-b"):
            d = scan_root / name
            d.mkdir()
            (d / ".git").write_text("gitdir: /fake/.git/worktrees/" + name)

        # Patch the per-entry helpers so that wt-a times out on get_branch
        # but wt-b succeeds (and is then skipped for having no PR).
        visited = []

        def fake_get_branch(path):
            visited.append(path.name)
            if path.name == "wt-a":
                # Simulate a timeout: run() would have returned (1,'','timeout')
                # which causes get_branch to return None
                return None
            return "my-feature-branch"

        def fake_get_remote_url(path):
            return "https://github.com/owner/repo.git"

        def fake_get_pr_state(repo, branch):
            return None  # no PR — skipped

        def fake_dir_age_days(path):
            return 10.0  # old enough

        with patch.object(_mod, "get_branch", side_effect=fake_get_branch), \
             patch.object(_mod, "get_remote_url", side_effect=fake_get_remote_url), \
             patch.object(_mod, "get_pr_state", side_effect=fake_get_pr_state), \
             patch.object(_mod, "dir_age_days", side_effect=fake_dir_age_days), \
             patch.object(_mod, "is_worktree", return_value=True), \
             patch.object(_mod, "LOG_FILE", tmp_path / "prune.log"):
            sys.argv = ["prune-pr-worktrees", "--projects-dir", str(scan_root), "--age-days", "0"]
            _mod.main()

        out = capsys.readouterr().out
        # Both directories were visited (get_branch called for each)
        assert "wt-a" in visited, "wt-a should have been visited"
        assert "wt-b" in visited, "wt-b should have been visited"
        # Summary line must be present — confirms loop completed instead of aborting
        assert "Summary:" in out
