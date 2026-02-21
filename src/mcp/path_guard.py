"""
Path Guard — structural guarantee that personal data never enters a git repo.

Provides assertions that fail loudly (PathGuardError) if a write path
resolves inside any git repository or outside the designated workspace.

Import-time validation: call validated_workspace() during module init
to crash early if the workspace is misconfigured.
"""

from pathlib import Path


class PathGuardError(RuntimeError):
    """Raised when a write path violates repo/workspace boundaries."""


def assert_not_in_git_repo(path: Path) -> None:
    """Raise PathGuardError if *path* is inside any git repository.

    Walks parent directories looking for a ``.git/`` directory.
    """
    resolved = path.resolve()
    current = resolved if resolved.is_dir() else resolved.parent
    while True:
        if (current / ".git").exists():
            raise PathGuardError(
                f"Path is inside a git repo: {path}  (repo root: {current})"
            )
        parent = current.parent
        if parent == current:
            break
        current = parent


def assert_in_workspace(path: Path, workspace: Path) -> None:
    """Raise PathGuardError if *path* does not resolve under *workspace*.

    Prevents ``../`` traversal attacks or misconfigured paths.
    """
    resolved = path.resolve()
    ws_resolved = workspace.resolve()
    try:
        resolved.relative_to(ws_resolved)
    except ValueError:
        raise PathGuardError(
            f"Path {path} is not inside workspace {workspace}"
        )


def validated_workspace(workspace: Path) -> Path:
    """Return *workspace* after asserting it is not inside a git repo.

    Intended to be called at import time so the process crashes immediately
    on misconfiguration rather than silently writing data into a repo.
    """
    assert_not_in_git_repo(workspace)
    return workspace
