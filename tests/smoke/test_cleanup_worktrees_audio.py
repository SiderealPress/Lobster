"""
Smoke tests: scripts/cleanup-worktrees-audio.sh

These tests verify correctness of the cleanup script without requiring a live
Telegram session, cron daemon, or real git worktrees that span the filesystem.
They exercise the actual shell script against controlled temporary fixtures.

Behaviors verified:

C1. Script exits 0 on a clean system where no audio files or stale worktrees exist.

C2. Audio files older than AUDIO_RETENTION_DAYS are deleted.

C3. Audio files newer than AUDIO_RETENTION_DAYS are preserved.

C4. Only recognised audio extensions are deleted (ogg, mp3, m4a, wav, oga);
    other file types in the audio directory are left untouched.

C5. Script does not fail if the audio directory is missing — it logs a skip
    message and exits 0.

C6. Script passes bash -n syntax check (catches deployment-breaking typos before
    the cron job silently breaks).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

CLEANUP_SCRIPT = Path(__file__).parents[2] / "scripts" / "cleanup-worktrees-audio.sh"

# Retention period the tests exercise — must match the script default.
AUDIO_RETENTION_DAYS = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_script(env: dict | None = None, **kwargs) -> subprocess.CompletedProcess:
    """Run the cleanup script with a fully isolated environment overlay.

    We pass a minimal env rather than inheriting the full process env so that
    paths like HOME, LOBSTER_MESSAGES, etc. don't bleed in from the test runner
    and cause the script to operate on real directories.
    """
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", str(CLEANUP_SCRIPT)],
        capture_output=True,
        text=True,
        env=base_env,
        **kwargs,
    )


def isolation_env(tmp_path: Path) -> dict:
    """Return a minimal env dict that keeps the script confined to tmp_path."""
    return {
        "LOBSTER_INSTALL_DIR": str(tmp_path / "lobster-nonexistent"),
        "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
        "LOBSTER_PROJECTS": str(tmp_path / "projects"),
        "LOBSTER_MESSAGES": str(tmp_path / "messages"),
    }


def make_old_file(path: Path, days_old: int = AUDIO_RETENTION_DAYS + 1) -> None:
    """Create a file and backdate its mtime so it appears `days_old` days old."""
    path.touch()
    old_time = time.time() - (days_old * 86400)
    os.utime(path, (old_time, old_time))


def make_new_file(path: Path, days_old: int = AUDIO_RETENTION_DAYS - 1) -> None:
    """Create a file dated within the retention window."""
    path.touch()
    recent_time = time.time() - (days_old * 86400)
    os.utime(path, (recent_time, recent_time))


# ---------------------------------------------------------------------------
# C6: Syntax check (run first — a broken script invalidates all other tests)
# ---------------------------------------------------------------------------

def test_script_has_no_syntax_errors():
    """C6: bash -n must exit 0 — a syntax error silently breaks cron."""
    result = subprocess.run(
        ["bash", "-n", str(CLEANUP_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Syntax error in cleanup script:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# C1: Clean-system no-op
# ---------------------------------------------------------------------------

def test_exits_zero_on_clean_system(tmp_path):
    """C1: Script exits 0 when audio dir is empty and no stale worktrees exist."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    env = isolation_env(tmp_path)
    env["CLEANUP_AUDIO_DIR"] = str(audio_dir)

    result = run_script(env=env)
    assert result.returncode == 0, (
        f"Expected exit 0 on clean system.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# C2: Old audio files are deleted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extension", ["ogg", "mp3", "m4a", "wav", "oga"])
def test_old_audio_file_is_deleted(tmp_path, extension):
    """C2: Audio files older than retention period are removed for each supported extension."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    old_file = audio_dir / f"voice_note_old.{extension}"
    make_old_file(old_file)
    assert old_file.exists(), "Pre-condition: file must exist before running script"

    env = isolation_env(tmp_path)
    env["CLEANUP_AUDIO_DIR"] = str(audio_dir)
    env["CLEANUP_AUDIO_RETENTION_DAYS"] = str(AUDIO_RETENTION_DAYS)

    result = run_script(env=env)
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    assert not old_file.exists(), (
        f"Old .{extension} file should have been deleted but still exists"
    )


# ---------------------------------------------------------------------------
# C3: Recent audio files are preserved
# ---------------------------------------------------------------------------

def test_recent_audio_file_is_preserved(tmp_path):
    """C3: Audio files within the retention window must not be deleted."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    recent_file = audio_dir / "voice_note_recent.ogg"
    make_new_file(recent_file)
    assert recent_file.exists()

    env = isolation_env(tmp_path)
    env["CLEANUP_AUDIO_DIR"] = str(audio_dir)
    env["CLEANUP_AUDIO_RETENTION_DAYS"] = str(AUDIO_RETENTION_DAYS)

    result = run_script(env=env)
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    assert recent_file.exists(), (
        "Recent audio file was incorrectly deleted (within retention window)"
    )


# ---------------------------------------------------------------------------
# C4: Non-audio files in audio directory are left alone
# ---------------------------------------------------------------------------

def test_non_audio_files_are_not_deleted(tmp_path):
    """C4: Only recognised audio extensions are removed; other files survive."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    # These must be old enough to be candidates if the script accidentally matches them
    txt_file = audio_dir / "transcript.txt"
    json_file = audio_dir / "metadata.json"
    make_old_file(txt_file)
    make_old_file(json_file)

    env = isolation_env(tmp_path)
    env["CLEANUP_AUDIO_DIR"] = str(audio_dir)
    env["CLEANUP_AUDIO_RETENTION_DAYS"] = str(AUDIO_RETENTION_DAYS)

    result = run_script(env=env)
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    assert txt_file.exists(), ".txt file should not have been deleted"
    assert json_file.exists(), ".json file should not have been deleted"


# ---------------------------------------------------------------------------
# C5: Missing audio directory is handled gracefully
# ---------------------------------------------------------------------------

def test_missing_audio_directory_is_skipped(tmp_path):
    """C5: Script exits 0 and logs a skip message when audio dir doesn't exist."""
    # Point CLEANUP_AUDIO_DIR at a path that definitely does not exist
    nonexistent_audio_dir = tmp_path / "definitely-absent" / "audio"
    assert not nonexistent_audio_dir.exists(), "Pre-condition: dir must not exist"

    env = isolation_env(tmp_path)
    env["CLEANUP_AUDIO_DIR"] = str(nonexistent_audio_dir)

    result = run_script(env=env)
    assert result.returncode == 0, (
        f"Script should exit 0 even when audio dir is missing.\nstderr: {result.stderr}"
    )
    assert "skipping" in result.stdout.lower(), (
        f"Expected a 'skipping' message in stdout when audio dir is absent.\n"
        f"Got stdout:\n{result.stdout}"
    )
