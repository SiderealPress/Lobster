"""
Filesystem utility functions for Lobster.

Canonical implementations of atomic file operations used across the codebase.
All functions are pure in the sense that they have no hidden dependencies —
their only side effects are the filesystem operations described in their names.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Atomically write JSON data to a file.

    Uses write-to-temp-then-rename pattern. On POSIX, rename() within the
    same filesystem is atomic, so readers never see a partial file.

    Args:
        path: Target file path.
        data: JSON-serializable data.
        indent: JSON indentation level.

    Raises:
        OSError: If the write or rename fails.
        TypeError: If data is not JSON-serializable.
    """
    # Serialize first (fail fast if not serializable)
    content = json.dumps(data, indent=indent)

    # Write to temp file in same directory (same filesystem = atomic rename)
    dir_path = str(path.parent)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # Force to disk before rename
        os.rename(tmp_path, str(path))
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def safe_move(src: Path, dest: Path) -> bool:
    """Safely move a file, ensuring source exists before moving.

    Returns True if moved, False if source was already gone (idempotent).
    Raises OSError on other failures.
    """
    try:
        src.rename(dest)
        return True
    except FileNotFoundError:
        # Source already moved (concurrent processing) — idempotent
        return False
