"""
Fireflies vault writer.

Idempotent writer that stores serialised FirefliesTranscript Markdown files
into the Obsidian vault at the correct path, skipping unchanged transcripts
(by comparing SHA-256 content hashes), and git-committing after each sync
run. Mirrors integrations.granola.vault_writer exactly, aside from the
"fireflies" vault subdirectory and commit message prefix.

Vault structure:
    ~/lobster-workspace/obsidian-vault/fireflies/YYYY/MM/{date}-{slug}.md

Git commit message format:
    fireflies: sync {N} transcripts [{ISO timestamp}]
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from integrations.fireflies.client import FirefliesTranscript
from integrations.fireflies.serializer import transcript_to_markdown, transcript_vault_path

log = logging.getLogger(__name__)

# Default vault location (can be overridden via FIREFLIES_VAULT_PATH env var or argument)
_DEFAULT_VAULT = Path.home() / "lobster-workspace" / "obsidian-vault"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(content: str) -> str:
    """Return the hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ensure_git_repo(vault_path: Path) -> None:
    """
    Initialise vault as a git repo if it isn't one already.
    No-op if .git/ already exists.
    """
    git_dir = vault_path / ".git"
    if git_dir.exists():
        return

    log.info("Initialising git repo in vault: %s", vault_path)
    subprocess.run(
        ["git", "init"],
        cwd=str(vault_path),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "lobster@localhost"],
        cwd=str(vault_path), check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Lobster"],
        cwd=str(vault_path), check=True, capture_output=True, text=True,
    )
    log.info("Git repo initialised in vault.")


def _git_commit(vault_path: Path, n_written: int, timestamp: str) -> bool:
    """
    Stage all changes and create a git commit.

    Returns True if a commit was made, False if there was nothing to commit.
    """
    add_result = subprocess.run(
        ["git", "add", "-A"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        log.warning("git add failed: %s", add_result.stderr)
        return False

    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if not status_result.stdout.strip():
        log.debug("Nothing to commit in vault.")
        return False

    msg = f"fireflies: sync {n_written} transcripts [{timestamp}]"
    commit_result = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        log.warning("git commit failed: %s", commit_result.stderr)
        return False

    log.info("Git commit: %s", msg)
    return True


# ---------------------------------------------------------------------------
# WriteResult
# ---------------------------------------------------------------------------


class WriteResult:
    """Summary of what the vault writer did during a sync run."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.skipped: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.committed: bool = False

    @property
    def n_written(self) -> int:
        return len(self.written)

    @property
    def n_skipped(self) -> int:
        return len(self.skipped)

    @property
    def n_errors(self) -> int:
        return len(self.errors)

    def __repr__(self) -> str:
        return (
            f"WriteResult(written={self.n_written}, skipped={self.n_skipped}, "
            f"errors={self.n_errors}, committed={self.committed})"
        )


# ---------------------------------------------------------------------------
# Main write function
# ---------------------------------------------------------------------------


def write_transcript(
    transcript: FirefliesTranscript,
    vault_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """
    Write a single transcript to the vault.

    Idempotent: if the file already exists with identical content
    (same SHA-256), the write is skipped.

    Returns:
        (was_written, message) — was_written is True if file was created/updated.
    """
    if vault_path is None:
        vault_env = os.environ.get("FIREFLIES_VAULT_PATH", "").strip()
        vault_path = Path(vault_env) if vault_env else _DEFAULT_VAULT

    rel_path = transcript_vault_path(transcript)
    abs_path = vault_path / rel_path

    abs_path.parent.mkdir(parents=True, exist_ok=True)

    content = transcript_to_markdown(transcript)
    new_hash = _sha256(content)

    if abs_path.exists():
        existing_content = abs_path.read_text(encoding="utf-8")
        existing_hash = _sha256(existing_content)
        if existing_hash == new_hash:
            log.debug("Transcript %s unchanged — skipping write", transcript.id)
            return False, "unchanged"

    abs_path.write_text(content, encoding="utf-8")
    log.info("Wrote transcript %s → %s", transcript.id, rel_path)
    return True, rel_path


def write_transcripts_batch(
    transcripts: list[FirefliesTranscript],
    vault_path: Optional[Path] = None,
    commit: bool = True,
) -> WriteResult:
    """
    Write a batch of transcripts to the vault, then optionally git-commit.

    Returns:
        WriteResult summarising what happened.
    """
    if vault_path is None:
        vault_env = os.environ.get("FIREFLIES_VAULT_PATH", "").strip()
        vault_path = Path(vault_env) if vault_env else _DEFAULT_VAULT

    vault_path.mkdir(parents=True, exist_ok=True)
    _ensure_git_repo(vault_path)

    result = WriteResult()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for transcript in transcripts:
        try:
            was_written, detail = write_transcript(transcript, vault_path=vault_path)
            if was_written:
                result.written.append(transcript.id)
            else:
                result.skipped.append(transcript.id)
        except Exception as exc:
            log.error("Failed to write transcript %s: %s", transcript.id, exc)
            result.errors.append((transcript.id, str(exc)))

    if commit and (result.written or result.errors):
        result.committed = _git_commit(vault_path, result.n_written, timestamp)

    log.info(
        "write_transcripts_batch: written=%d skipped=%d errors=%d committed=%s",
        result.n_written, result.n_skipped, result.n_errors, result.committed,
    )
    return result
