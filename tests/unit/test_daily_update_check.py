"""
Behavioral test for scripts/daily-update-check.sh (issue #2287).

The script used to gate the "UPDATE AVAILABLE" notice on HEAD != origin/main
and then report the directional behind count. When local main is ahead of
origin/main (e.g. after an upgrade merge), the SHAs differ but behind is 0, so
the inbox got "Lobster is 0 commits behind origin/main" every night.

Each test builds a throwaway origin repo plus a clone acting as the install
dir, runs the real script against it, and inspects the inbox.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent.parent
SCRIPT = REPO_DIR / "scripts" / "daily-update-check.sh"
JSON_LIB = REPO_DIR / "scripts" / "lib" / "json_message.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("jq") and shutil.which("bash")),
    reason="needs git, jq and bash",
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        # Keep the user's global config (gpgsign, hooksPath, templates) out.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@pytest.fixture
def setup(tmp_path: Path):
    """Return (upstream, install, inbox, run) for one isolated scenario."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    _git(upstream, "symbolic-ref", "HEAD", "refs/heads/main")
    _commit(upstream, "base")

    install = tmp_path / "lobster"
    _git(tmp_path, "clone", "-q", str(upstream), str(install))
    lib_dir = install / "scripts" / "lib"
    lib_dir.mkdir(parents=True)
    shutil.copy(JSON_LIB, lib_dir / "json_message.sh")

    messages = tmp_path / "messages"
    inbox = messages / "inbox"
    inbox.mkdir(parents=True)

    def run() -> list[dict]:
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "LOBSTER_INSTALL_DIR": str(install),
            "LOBSTER_MESSAGES": str(messages),
            "LOBSTER_CONFIG_DIR": str(tmp_path / "no-config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
        return [json.loads(p.read_text()) for p in sorted(inbox.glob("*.json"))]

    return upstream, install, inbox, run


def test_up_to_date_emits_nothing(setup):
    _, _, _, run = setup
    assert run() == []


def test_local_ahead_of_origin_emits_nothing(setup):
    """The #2287 false positive: ahead-only must not claim an update."""
    _, install, _, run = setup
    _commit(install, "local-only")
    assert run() == []


def test_behind_reports_real_count(setup):
    upstream, _, _, run = setup
    _commit(upstream, "u1")
    _commit(upstream, "u2")
    msgs = run()
    assert len(msgs) == 1
    assert re.search(r"\b2 commits? behind origin/main", msgs[0]["text"])
    assert msgs[0]["source"] == "system"


def test_diverged_reports_behind_count(setup):
    upstream, install, _, run = setup
    _commit(upstream, "u1")
    _commit(install, "local-only")
    msgs = run()
    assert len(msgs) == 1
    assert re.search(r"\b1 commits? behind origin/main", msgs[0]["text"])
