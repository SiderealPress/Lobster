"""
Granola vault writer — Slice 2.

Idempotent writer that stores serialised GranolaNote Markdown files
into the Obsidian vault at the correct path, skipping unchanged notes
(by comparing SHA-256 content hashes), and git-committing after each
sync run.

Vault structure:
    ~/lobster-workspace/obsidian-vault/granola/YYYY/MM/{date}-{slug}.md

Git commit message format:
    granola: sync {N} notes [{ISO timestamp}]
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from integrations.granola.client import GranolaNote
from integrations.granola.serializer import note_to_markdown, note_vault_path

log = logging.getLogger(__name__)

# Default vault location (can be overridden via GRANOLA_VAULT_PATH env var or argument)
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
    # Set a safe default identity (non-interactive environments often lack one)
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
    # Stage all changes
    add_result = subprocess.run(
        ["git", "add", "-A"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        log.warning("git add failed: %s", add_result.stderr)
        return False

    # Check if there's anything staged
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
    )
    if not status_result.stdout.strip():
        log.debug("Nothing to commit in vault.")
        return False

    msg = f"granola: sync {n_written} notes [{timestamp}]"
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
        self.written: list[str] = []     # note IDs that were new/changed and written
        self.skipped: list[str] = []     # note IDs unchanged (content hash match)
        self.errors: list[tuple[str, str]] = []  # (note_id, error_message)
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


def write_note(
    note: GranolaNote,
    vault_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """
    Write a single note to the vault.

    Idempotent: if the file already exists with identical content
    (same SHA-256), the write is skipped.

    Args:
        note:       The note to write.
        vault_path: Root of the Obsidian vault. Defaults to _DEFAULT_VAULT.
                    Can also be overridden via GRANOLA_VAULT_PATH env var.

    Returns:
        (was_written, message)  — was_written is True if file was created/updated.
    """
    if vault_path is None:
        vault_env = os.environ.get("GRANOLA_VAULT_PATH", "").strip()
        vault_path = Path(vault_env) if vault_env else _DEFAULT_VAULT

    rel_path = note_vault_path(note)
    abs_path = vault_path / rel_path

    # Ensure parent directory exists
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialise
    content = note_to_markdown(note)
    new_hash = _sha256(content)

    # Compare with existing file
    if abs_path.exists():
        existing_content = abs_path.read_text(encoding="utf-8")
        existing_hash = _sha256(existing_content)
        if existing_hash == new_hash:
            log.debug("Note %s unchanged — skipping write", note.id)
            return False, "unchanged"

    # Write
    abs_path.write_text(content, encoding="utf-8")
    log.info("Wrote note %s → %s", note.id, rel_path)
    return True, rel_path


def write_notes_batch(
    notes: list[GranolaNote],
    vault_path: Optional[Path] = None,
    commit: bool = True,
) -> WriteResult:
    """
    Write a batch of notes to the vault, then optionally git-commit.

    Args:
        notes:      List of GranolaNote objects to write.
        vault_path: Root of the Obsidian vault. Defaults to _DEFAULT_VAULT.
        commit:     If True, git-commit after writing.

    Returns:
        WriteResult summarising what happened.
    """
    if vault_path is None:
        vault_env = os.environ.get("GRANOLA_VAULT_PATH", "").strip()
        vault_path = Path(vault_env) if vault_env else _DEFAULT_VAULT

    # Ensure vault dir exists
    vault_path.mkdir(parents=True, exist_ok=True)

    # Ensure it's a git repo
    _ensure_git_repo(vault_path)

    result = WriteResult()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for note in notes:
        try:
            was_written, detail = write_note(note, vault_path=vault_path)
            if was_written:
                result.written.append(note.id)
            else:
                result.skipped.append(note.id)
        except Exception as exc:
            log.error("Failed to write note %s: %s", note.id, exc)
            result.errors.append((note.id, str(exc)))

    if commit and (result.written or result.errors):
        result.committed = _git_commit(vault_path, result.n_written, timestamp)

    log.info(
        "write_notes_batch: written=%d skipped=%d errors=%d committed=%s",
        result.n_written, result.n_skipped, result.n_errors, result.committed,
    )
    return result
