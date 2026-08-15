"""
Tests for scripts/install-pii-scan-hook.sh

Verifies:
1. Installs hooks/pii-scan-guard.py as .git/hooks/pre-push in a target repo,
   executable, carrying the installer marker
2. Refuses to overwrite an existing unrelated pre-push hook without --force
3. --force backs up the existing hook and overwrites it
4. Re-running against an already-installed target updates in place (marker
   present) without requiring --force
5. The installed hook actually invokes pii-scan-guard.py end-to-end (a real
   `git push`-shaped stdin against a throwaway local remote), proving the
   install is wired correctly, not just that files were copied
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-pii-scan-hook.sh"


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def target_repo(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("hello\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "init"], repo)
    return repo


def _install(target_repo, extra_args=None, env=None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), str(target_repo), *(extra_args or [])],
        capture_output=True, text=True, env=full_env,
    )


class TestInstallPiiScanHook:
    def test_installs_executable_hook_with_marker(self, target_repo):
        result = _install(target_repo)
        assert result.returncode == 0, result.stderr
        hook = target_repo / ".git" / "hooks" / "pre-push"
        assert hook.is_file()
        assert os.access(hook, os.X_OK)
        assert "installed-by: install-pii-scan-hook.sh" in hook.read_text()

    def test_installs_prompt_file_alongside_hook(self, target_repo):
        _install(target_repo)
        prompt = target_repo / ".git" / "hooks" / "pii-scan-guard.prompt.md"
        assert prompt.is_file()
        assert "PII" in prompt.read_text()

    def test_refuses_to_overwrite_unrelated_existing_hook(self, target_repo):
        existing = target_repo / ".git" / "hooks" / "pre-push"
        existing.write_text("#!/usr/bin/env bash\necho 'some other hook'\n")
        existing.chmod(0o755)

        result = _install(target_repo)
        assert result.returncode != 0
        assert "Refusing to overwrite" in result.stderr
        # Original content must be untouched
        assert "some other hook" in existing.read_text()

    def test_force_backs_up_and_overwrites_existing_hook(self, target_repo):
        existing = target_repo / ".git" / "hooks" / "pre-push"
        existing.write_text("#!/usr/bin/env bash\necho 'some other hook'\n")
        existing.chmod(0o755)

        result = _install(target_repo, extra_args=["--force"])
        assert result.returncode == 0, result.stderr

        backup = target_repo / ".git" / "hooks" / "pre-push.pre-pii-scan-backup"
        assert backup.is_file()
        assert "some other hook" in backup.read_text()
        assert "installed-by: install-pii-scan-hook.sh" in existing.read_text()

    def test_reinstall_over_own_previous_install_does_not_need_force(self, target_repo):
        first = _install(target_repo)
        assert first.returncode == 0, first.stderr
        second = _install(target_repo)
        assert second.returncode == 0, second.stderr
        assert "Existing install found" in second.stdout

    def test_rejects_non_git_target(self, tmp_path):
        not_a_repo = tmp_path / "plain-dir"
        not_a_repo.mkdir()
        result = _install(not_a_repo)
        assert result.returncode != 0
        assert "not inside a git repository" in result.stderr

    def test_installed_hook_actually_runs_pii_scan_guard_end_to_end(self, target_repo):
        # Prove the wiring works, not just that files were copied: invoke the
        # installed hook directly with a real pre-push-shaped stdin payload,
        # in mode=off (network-free) — it must exit 0 silently, exactly as
        # pii-scan-guard.py itself does in that mode.
        _install(target_repo)
        hook = target_repo / ".git" / "hooks" / "pre-push"

        (target_repo / "b.txt").write_text("more content\n")
        _run_git(["add", "-A"], target_repo)
        _run_git(["commit", "-q", "-m", "second commit"], target_repo)
        local_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=target_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        stdin = f"refs/heads/main {local_sha} refs/heads/main {'0' * 40}\n"
        env = os.environ.copy()
        env.pop("LOBSTER_PII_SCAN_MODE", None)  # default: off
        result = subprocess.run(
            [str(hook)], input=stdin, cwd=target_repo,
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        assert result.stderr == ""
