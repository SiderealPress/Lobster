"""
Unit tests for scripts/prune-pr-worktrees.py.

Focus areas (per issue #1826):
  1. Timeout resilience — a subprocess timeout on one directory must NOT abort
     the scan; the script logs the timeout and continues to the next directory.
     Timed-out directories are counted separately from "no PR found" skips.
  2. Fallback rm gating — the direct shutil.rmtree fallback fires only when the
     worktree is confirmed absent from git's own registry (git worktree list).
     When git still knows about the worktree, the fallback must NOT fire.

Named after behaviors, not mechanisms.
"""
from __future__ import annotations

import importlib
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Import the module under test.
# The script lives at scripts/prune-pr-worktrees.py and is not a package, so
# we import it via importlib using a direct file path.
# ---------------------------------------------------------------------------

SCRIPT_PATH = (
    Path(__file__).parents[3] / "scripts" / "prune-pr-worktrees.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prune_pr_worktrees", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    return _load_module()


# ---------------------------------------------------------------------------
# Helper: a minimal fake Path that looks like a worktree directory.
# ---------------------------------------------------------------------------

def _fake_worktree_dir(tmp_path: Path, name: str, branch: str = "feat/test-branch") -> Path:
    """Create a minimal fake worktree directory under tmp_path."""
    d = tmp_path / name
    d.mkdir()
    # .git file marks it as a linked worktree
    git_file = d / ".git"
    main_git = tmp_path / "main_repo" / ".git" / "worktrees" / name
    main_git.mkdir(parents=True, exist_ok=True)
    git_file.write_text(f"gitdir: {main_git}\n")
    return d


# ===========================================================================
# 1. Timeout resilience
# ===========================================================================


class TestTimeoutDoesNotAbortScan:
    """A subprocess timeout on one directory must not abort the rest of the scan."""

    def test_scan_continues_after_gh_pr_list_timeout(self, mod: ModuleType, tmp_path: Path) -> None:
        """
        Given two worktree dirs where gh pr list times out on the first,
        the second directory should still be processed and the summary logged.
        Spec: issue #1826 — TimeoutExpired must be caught, logged, and the loop
        must continue.
        """
        dir_a = _fake_worktree_dir(tmp_path, "dir-a")
        dir_b = _fake_worktree_dir(tmp_path, "dir-b")

        # Make dir_a old enough to pass age check
        import os, time
        old_mtime = time.time() - (8 * 86400)  # 8 days old
        os.utime(str(dir_a), (old_mtime, old_mtime))
        os.utime(str(dir_b), (old_mtime, old_mtime))

        call_count = {"n": 0}

        def fake_run(cmd, cwd=None, timeout=30):
            call_count["n"] += 1
            cmd_str = " ".join(str(c) for c in cmd)
            if "branch" in cmd_str and "--show-current" in cmd_str:
                return 0, "feat/test-branch", ""
            if "remote" in cmd_str and "get-url" in cmd_str:
                return 0, "https://github.com/owner/repo.git", ""
            if "gh" in cmd_str and "pr" in cmd_str and "list" in cmd_str:
                if cwd is None or str(dir_a) in str(cwd or ""):
                    # Simulate timeout: return the timeout sentinel
                    return 1, "", "timeout"
                # dir_b gets a normal MERGED response
                return 0, '[{"state":"MERGED"}]', ""
            if "worktree" in cmd_str and "remove" in cmd_str:
                return 0, "", ""
            if "worktree" in cmd_str and "prune" in cmd_str:
                return 0, "", ""
            return 0, "", ""

        log_messages = []

        def fake_log(msg: str) -> None:
            log_messages.append(msg)

        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "log", side_effect=fake_log), \
             patch.object(mod, "find_main_repo", return_value=tmp_path / "main_repo"), \
             patch.object(mod, "DEFAULT_PROJECTS_DIR", tmp_path):
            import argparse
            args = argparse.Namespace(dry_run=True, age_days=7.0, projects_dir=tmp_path)
            # Patch sys.argv parse if main() is called, but we test main() directly
            with patch("sys.argv", ["prune-pr-worktrees.py", "--dry-run", "--projects-dir", str(tmp_path)]):
                mod.main()

        # Summary must be emitted (scan was not aborted)
        summary_lines = [m for m in log_messages if "Summary:" in m]
        assert len(summary_lines) == 1, "Summary line must always be emitted"

        # dir_b (MERGED) must have been processed despite dir_a timing out
        prune_lines = [m for m in log_messages if "[PRUNE]" in m or "[DRY-RUN]" in m]
        assert len(prune_lines) >= 1, "dir_b (MERGED) should have been pruned despite dir_a timeout"

    def test_timeout_counted_separately_from_no_pr(self, mod: ModuleType, tmp_path: Path) -> None:
        """
        Timeout-skipped directories must be recorded so the summary can distinguish
        timeouts from true 'no PR found' cases.
        Spec: issue #1826 — a timeout log entry must appear in the log, not just
        a silent 'no PR' skip.

        This test exercises the real run() implementation so that the [TIMEOUT]
        log line emitted inside run() is genuinely produced, not asserted against
        dead air from a fully-patched run().  subprocess.Popen is mocked at the
        boundary: git commands succeed normally; the gh pr list call raises
        TimeoutExpired to simulate a real subprocess timeout.
        """
        dir_a = _fake_worktree_dir(tmp_path, "timeout-dir")

        import os, time
        old_mtime = time.time() - (8 * 86400)
        os.utime(str(dir_a), (old_mtime, old_mtime))

        def make_popen_mock(cmd, **kwargs):
            """Return a mock Popen object whose communicate() behaviour depends on cmd."""
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            cmd_str = " ".join(str(c) for c in cmd)
            if "branch" in cmd_str and "--show-current" in cmd_str:
                mock_proc.communicate.return_value = ("feat/test-branch\n", "")
            elif "remote" in cmd_str and "get-url" in cmd_str:
                mock_proc.communicate.return_value = ("https://github.com/owner/repo.git\n", "")
            elif "gh" in cmd_str and "pr" in cmd_str:
                # Simulate a real subprocess timeout on gh pr list
                mock_proc.communicate.side_effect = subprocess.TimeoutExpired(
                    cmd=cmd, timeout=30
                )
                # Second communicate() call (after kill) must succeed to reap the process
                mock_proc.communicate.side_effect = [
                    subprocess.TimeoutExpired(cmd=cmd, timeout=30),
                    ("", ""),
                ]
            else:
                mock_proc.communicate.return_value = ("", "")
            return mock_proc

        log_messages = []

        def fake_log(msg: str) -> None:
            log_messages.append(msg)

        with patch("subprocess.Popen", side_effect=make_popen_mock), \
             patch.object(mod, "log", side_effect=fake_log), \
             patch.object(mod, "DEFAULT_PROJECTS_DIR", tmp_path):
            with patch("sys.argv", ["prune-pr-worktrees.py", "--dry-run", "--projects-dir", str(tmp_path)]):
                mod.main()

        # The [TIMEOUT] line must have been emitted by the real run() implementation,
        # confirming the timeout code path was genuinely exercised — not mocked away.
        timeout_lines = [m for m in log_messages if "[TIMEOUT]" in m]
        assert len(timeout_lines) >= 1, (
            "Expected at least one [TIMEOUT] log entry emitted by run() "
            "when subprocess raises TimeoutExpired on gh pr list"
        )


# ===========================================================================
# 2. Fallback rm gating — only fire when worktree is absent from git registry
# ===========================================================================

TIMEOUT_SENTINEL = "timeout"


class TestFallbackRmGating:
    """The direct rm fallback must only fire when git no longer knows about the worktree.

    The current code checks `if worktree_path.exists()` before calling shutil.rmtree,
    but does NOT gate on whether the worktree is still registered in git's list.
    These tests specify the desired behavior: the registry check must gate the fallback.
    They are RED against the current implementation and GREEN after the fix.
    """

    def test_fallback_does_not_rm_when_worktree_still_registered(
        self, mod: ModuleType, tmp_path: Path
    ) -> None:
        """
        When git worktree remove fails AND the worktree is still present in
        'git worktree list --porcelain', the fallback rm must NOT execute.
        Spec: issue #1826 — restrict fallback rm to orphaned directories only.

        Setup: tmp_path IS the DEFAULT_PROJECTS_DIR so the path guard passes.
        The only thing that should block rm is the registry check.
        """
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        worktree_path = tmp_path / "my-feature"
        worktree_path.mkdir()

        # Simulate: worktree still registered in git (porcelain output contains path)
        worktree_list_output = f"worktree {worktree_path}\nHEAD abc123\nbranch refs/heads/feat/test\n"

        def fake_run(cmd, cwd=None, timeout=30):
            cmd_str = " ".join(str(c) for c in cmd)
            if "worktree" in cmd_str and "remove" in cmd_str:
                return 1, "", "fatal: not a git repository"
            if "worktree" in cmd_str and "list" in cmd_str and "porcelain" in cmd_str:
                return 0, worktree_list_output, ""
            if "worktree" in cmd_str and "prune" in cmd_str:
                return 0, "", ""
            return 0, "", ""

        log_messages = []
        rmtree_called = False

        import shutil as shutil_mod

        def fake_rmtree(path, *args, **kwargs):
            nonlocal rmtree_called
            rmtree_called = True

        # Patch DEFAULT_PROJECTS_DIR to tmp_path so path guard passes — only registry
        # check should block rm.
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "log", side_effect=lambda m: log_messages.append(m)), \
             patch.object(mod, "DEFAULT_PROJECTS_DIR", tmp_path), \
             patch.object(shutil_mod, "rmtree", side_effect=fake_rmtree):
            result = mod.remove_worktree(main_repo, worktree_path, dry_run=False)

        assert not rmtree_called, (
            "shutil.rmtree must NOT be called when the worktree is still registered in git"
        )
        # Removal failed (correctly) because worktree is still registered
        assert result is False

    def test_fallback_rm_fires_when_worktree_absent_from_registry(
        self, mod: ModuleType, tmp_path: Path
    ) -> None:
        """
        When git worktree remove fails AND 'git worktree list --porcelain' does NOT
        contain the worktree path, the fallback rm SHOULD execute (orphaned dir).
        Spec: issue #1826 — orphaned directories (absent from registry) are safe to remove.
        """
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        worktree_path = tmp_path / "orphaned-feature"
        worktree_path.mkdir()

        # Simulate: worktree NOT in git's registry (git already forgot it)
        worktree_list_output = "worktree /some/other/path\nHEAD abc123\nbranch refs/heads/other\n"

        def fake_run(cmd, cwd=None, timeout=30):
            cmd_str = " ".join(str(c) for c in cmd)
            if "worktree" in cmd_str and "remove" in cmd_str:
                return 1, "", "fatal: not a git repository"
            if "worktree" in cmd_str and "list" in cmd_str and "porcelain" in cmd_str:
                return 0, worktree_list_output, ""
            if "worktree" in cmd_str and "prune" in cmd_str:
                return 0, "", ""
            return 0, "", ""

        log_messages = []
        rmtree_called = {"path": None}

        import shutil as shutil_mod

        def fake_rmtree(path, *args, **kwargs):
            rmtree_called["path"] = path

        # Patch DEFAULT_PROJECTS_DIR to tmp_path so path guard passes
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "log", side_effect=lambda m: log_messages.append(m)), \
             patch.object(mod, "DEFAULT_PROJECTS_DIR", tmp_path), \
             patch.object(shutil_mod, "rmtree", side_effect=fake_rmtree):
            result = mod.remove_worktree(main_repo, worktree_path, dry_run=False)

        assert rmtree_called["path"] is not None, (
            "shutil.rmtree MUST be called when the worktree is absent from git's registry (orphaned)"
        )
        assert result is True

    def test_fallback_rm_still_blocked_by_path_guard_even_for_orphaned_dir(
        self, mod: ModuleType, tmp_path: Path
    ) -> None:
        """
        Even when the worktree is absent from git's registry, the existing path
        safety guard (must be under DEFAULT_PROJECTS_DIR) must still block removal
        if the path is outside the expected root.
        Spec: defense-in-depth — both the registry check AND the path guard apply.
        """
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        # Path outside any expected projects dir
        dangerous_path = Path("/tmp/important-data")
        dangerous_path.mkdir(exist_ok=True)

        worktree_list_output = "worktree /some/other/path\nHEAD abc123\n"

        def fake_run(cmd, cwd=None, timeout=30):
            cmd_str = " ".join(str(c) for c in cmd)
            if "worktree" in cmd_str and "remove" in cmd_str:
                return 1, "", "fatal: not a git repository"
            if "worktree" in cmd_str and "list" in cmd_str and "porcelain" in cmd_str:
                return 0, worktree_list_output, ""
            if "worktree" in cmd_str and "prune" in cmd_str:
                return 0, "", ""
            return 0, "", ""

        log_messages = []
        rmtree_called = False

        import shutil as shutil_mod

        def fake_rmtree(path, *args, **kwargs):
            nonlocal rmtree_called
            rmtree_called = True

        # DEFAULT_PROJECTS_DIR is tmp_path; dangerous_path is outside it
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "log", side_effect=lambda m: log_messages.append(m)), \
             patch.object(mod, "DEFAULT_PROJECTS_DIR", tmp_path), \
             patch.object(shutil_mod, "rmtree", side_effect=fake_rmtree):
            result = mod.remove_worktree(main_repo, dangerous_path, dry_run=False)

        assert not rmtree_called, "Path guard must block rm even for orphaned dirs outside projects root"
        assert result is False
        error_lines = [m for m in log_messages if "[ERROR]" in m and "refusing" in m]
        assert len(error_lines) >= 1, "Must log an error when refusing removal due to path guard"


# ===========================================================================
# 3. run() function: timeout handling at subprocess level
# ===========================================================================


class TestRunTimeoutHandling:
    """The run() helper must handle TimeoutExpired gracefully."""

    def test_run_catches_timeout_and_returns_sentinel(self, mod: ModuleType) -> None:
        """
        run() must catch subprocess.TimeoutExpired, kill the process, and return
        (1, '', 'timeout') so callers can handle it without an unhandled exception.
        Spec: issue #1826 — TimeoutExpired must not propagate from run().
        """
        # Use a command that will actually hang briefly — we mock Popen instead
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["sleep", "100"], timeout=30)
        mock_proc.pid = 12345

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(mod, "log") as mock_log:
            # After TimeoutExpired, communicate is called again (to reap)
            # We need the second call to succeed
            mock_proc.communicate.side_effect = [
                subprocess.TimeoutExpired(cmd=["sleep", "100"], timeout=30),
                ("", ""),  # second call (reap) succeeds
            ]
            rc, stdout, stderr = mod.run(["sleep", "100"], timeout=1)

        assert rc == 1
        assert stdout == ""
        assert stderr == "timeout"
        mock_proc.kill.assert_called_once()

    def test_run_timeout_does_not_raise(self, mod: ModuleType) -> None:
        """
        Calling run() when the subprocess times out must never raise an exception —
        only return the (1, '', 'timeout') sentinel.
        """
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=30),
            ("", ""),
        ]

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(mod, "log"):
            try:
                result = mod.run(["git", "fetch"], timeout=1)
            except Exception as exc:
                pytest.fail(f"run() raised an exception on timeout: {exc}")

        assert result[0] == 1
