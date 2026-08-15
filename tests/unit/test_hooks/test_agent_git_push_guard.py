"""
Unit tests for hooks/agent-git-push-guard.py.

These tests build real temporary git repositories (no mocking of git itself)
and invoke the hook's real main() against realistic PreToolUse stdin JSON,
the same contract Claude Code uses. This proves the hook's actual behavior
end-to-end at the git-plumbing level: scope guard, diff computation, finding
detection, blocking (exit 2 + stderr), the retry-counter fail-open bound, and
silent allow on a clean push -- not just that its helper functions return the
right values in isolation.
"""
import importlib.util
import json
import subprocess
import sys
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_hook():
    hooks_dir = Path(__file__).parent.parent.parent.parent / "hooks"
    # git_push_scan must be importable by its module name when the hook does
    # `sys.path.insert(0, ...); import git_push_scan`.
    sys.path.insert(0, str(hooks_dir))
    spec = importlib.util.spec_from_file_location("agent_git_push_guard", hooks_dir / "agent-git-push-guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


def _run(stdin_data: dict):
    stdin_str = json.dumps(stdin_data)
    captured_stderr = StringIO()
    exit_code = None
    with patch("sys.stdin", StringIO(stdin_str)), patch("sys.stderr", captured_stderr):
        try:
            hook.main()
        except SystemExit as e:
            exit_code = e.code
    return exit_code, captured_stderr.getvalue()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with an `origin` remote, pushed base commit (so
    origin/main exists as a local ref for the fallback diff path), whose
    remote URL can be changed per-test to simulate different target repos."""
    bare = tmp_path / "bare_remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"file://{bare}")

    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "push", "origin", "main")

    yield repo

    # Clean up any fire-count state this test may have written.
    for f in Path("/tmp").glob("lobster-git-push-guard-fires-*"):
        f.unlink(missing_ok=True)


def _set_remote(repo: Path, url: str):
    _git(repo, "remote", "set-url", "origin", url)


def _add_commit(repo: Path, filename: str, content: str, message: str = "wip"):
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)


def _stdin_for(repo: Path, command: str = "git push origin main") -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo),
        "session_id": f"test-{uuid.uuid4()}",
    }


# ---------------------------------------------------------------------------
# Non-git-push / non-target-repo cases: always allow, silently
# ---------------------------------------------------------------------------

def test_non_bash_tool_is_ignored(git_repo):
    exit_code, stderr = _run({"tool_name": "Write", "tool_input": {}})
    assert exit_code == 0
    assert stderr == ""


def test_non_push_bash_command_is_ignored(git_repo):
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "leak.py", 'email = "real@example.org"\n')
    stdin = _stdin_for(git_repo, command="git status")
    exit_code, stderr = _run(stdin)
    assert exit_code == 0
    assert stderr == ""


def test_push_to_non_target_repo_is_skipped_even_with_pii(git_repo):
    _set_remote(git_repo, "https://github.com/someone-else/other-repo.git")
    _add_commit(git_repo, "leak.py", 'ssn = "123-45-6789"\n')
    exit_code, stderr = _run(_stdin_for(git_repo))
    assert exit_code == 0
    assert stderr == ""


# ---------------------------------------------------------------------------
# Target repo: clean push allowed silently
# ---------------------------------------------------------------------------

def test_clean_push_to_target_repo_is_allowed_silently(git_repo):
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "clean.py", "def mul(a, b):\n    return a * b\n")
    exit_code, stderr = _run(_stdin_for(git_repo))
    assert exit_code == 0
    assert stderr == ""


def test_ssh_style_remote_url_matches_target_repo(git_repo):
    _set_remote(git_repo, "git@github.com:SiderealPress/lobster.git")
    _add_commit(git_repo, "leak.py", 'ssn = "123-45-6789"\n')
    exit_code, stderr = _run(_stdin_for(git_repo))
    assert exit_code == 2
    assert "BLOCKED" in stderr


# ---------------------------------------------------------------------------
# Target repo with findings: blocked
# ---------------------------------------------------------------------------

def test_push_with_ssn_shaped_string_is_blocked(git_repo):
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "leak.py", 'user_ssn = "123-45-6789"\n')
    exit_code, stderr = _run(_stdin_for(git_repo))
    assert exit_code == 2
    assert "BLOCKED" in stderr
    assert "Social Security Number" in stderr
    assert "leak.py" in stderr


def test_push_with_secret_shaped_string_is_blocked(git_repo):
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "config.py", 'API_KEY = "sk-abcdefghijklmnopqrstuvwx"\n')
    exit_code, stderr = _run(_stdin_for(git_repo))
    assert exit_code == 2
    assert "BLOCKED" in stderr
    assert "API secret key" in stderr


def test_chained_command_with_push_is_still_detected(git_repo):
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "leak.py", 'ssn = "123-45-6789"\n')
    stdin = _stdin_for(git_repo, command='git add -A && git commit -m "x" && git push origin main')
    exit_code, stderr = _run(stdin)
    assert exit_code == 2


# ---------------------------------------------------------------------------
# Retry-counter / fail-open bound
# ---------------------------------------------------------------------------

def test_fail_open_after_max_hook_fires(git_repo):
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "leak.py", 'ssn = "123-45-6789"\n')
    stdin = _stdin_for(git_repo)
    # Same session_id across all fires -- this is what makes them count
    # against the same MAX_HOOK_FIRES bound.
    session_id = stdin["session_id"]

    seen_exit_codes = []
    for _ in range(hook.MAX_HOOK_FIRES + 1):
        exit_code, stderr = _run(dict(stdin, session_id=session_id))
        seen_exit_codes.append(exit_code)

    # First MAX_HOOK_FIRES fires block; the one after that fails open.
    assert seen_exit_codes[: hook.MAX_HOOK_FIRES] == [2] * hook.MAX_HOOK_FIRES
    assert seen_exit_codes[hook.MAX_HOOK_FIRES] == 0


def test_clean_retry_after_fix_clears_fire_state(git_repo):
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "leak.py", 'ssn = "123-45-6789"\n')
    stdin = _stdin_for(git_repo)

    exit_code, _ = _run(stdin)
    assert exit_code == 2

    # Agent "fixes" the finding by amending it out, then retries with the same session.
    _git(git_repo, "commit", "--amend", "-m", "wip (fixed)")
    (git_repo / "leak.py").write_text("# no secrets here\n")
    _git(git_repo, "add", "leak.py")
    _git(git_repo, "commit", "-m", "actually fixed")

    exit_code, stderr = _run(stdin)
    assert exit_code == 0
    assert stderr == ""

    fire_path = Path(f"/tmp/lobster-git-push-guard-fires-{stdin['session_id']}")
    assert not fire_path.exists()


# ---------------------------------------------------------------------------
# Falsifiability check
# ---------------------------------------------------------------------------

def test_blocking_behavior_is_falsifiable(monkeypatch, git_repo):
    """If git_push_scan.scan_diff were broken to always report no findings,
    the guard would silently let PII-shaped pushes through. This test proves
    that failure mode is exactly what the tests above would catch."""
    _set_remote(git_repo, "https://github.com/SiderealPress/lobster.git")
    _add_commit(git_repo, "leak.py", 'ssn = "123-45-6789"\n')

    monkeypatch.setattr(hook.git_push_scan, "scan_diff", lambda *a, **kw: ([], False))
    exit_code, stderr = _run(_stdin_for(git_repo))
    assert exit_code == 0, "broken scanner would wrongly allow a PII-bearing push through"
    assert stderr == ""
