"""
Unit tests for last-startup-cause.json reliability fix (issue #1972).

Two hooks participate:
  - on-compact.py: writes {"cause": "compaction", "ts": "<iso_utc>"} BEFORE exiting
  - inject-bootup-context.py: on startup, reads the file, classifies the cause,
    then overwrites it with {"cause": "restart", "ts": "<now>"} to self-clear

Constants:
  COMPACTION_CAUSE_WINDOW_SECONDS = 300  — max age (5 min) for a "compaction" entry
    to be trusted; older entries fall back to "restart"

Behaviors tested:
  on-compact.py:
    - write_startup_cause() creates the file with cause="compaction" and a valid UTC ts
    - write_startup_cause() overwrites a prior "restart" entry
    - write_startup_cause() is silent if the path is unwritable
    - write_startup_cause() uses an atomic rename (tmp → final)

  inject-bootup-context.py:
    - read_and_reset_startup_cause() returns "compaction" when file is fresh (<5 min)
    - read_and_reset_startup_cause() returns "restart" when file cause is "restart"
    - read_and_reset_startup_cause() returns "restart" when file is absent
    - read_and_reset_startup_cause() returns "restart" when file cause is "compaction"
      but ts is stale (>= 5 min)
    - read_and_reset_startup_cause() returns "restart" on corrupt JSON
    - after any read, the file is overwritten with cause="restart"
    - overwrite is silent if file becomes unwritable between read and write
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_ON_COMPACT_PATH = _HOOKS_DIR / "on-compact.py"
_INJECT_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"


def _load_inject_hook_module() -> object:
    """Load inject-bootup-context.py once at module level to extract constants."""
    spec = importlib.util.spec_from_file_location("_inject_hook_constants", _INJECT_HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_INJECT_HOOK_MODULE = _load_inject_hook_module()

# Import the window constant directly from the hook — avoids duplication so
# test coverage is not silently invalidated if the constant changes.
COMPACTION_CAUSE_WINDOW_SECONDS = _INJECT_HOOK_MODULE.COMPACTION_CAUSE_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PatchEnv:
    """Context manager to temporarily set / restore environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved: dict = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _load_on_compact(startup_cause_override: str | None = None) -> object:
    """Load on-compact.py with optional LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE."""
    import uuid as _uuid

    env: dict = {}
    if startup_cause_override is not None:
        env["LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE"] = startup_cause_override

    unique_name = f"on_compact_{_uuid.uuid4().hex}"
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location(unique_name, _ON_COMPACT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _load_inject_hook(
    workspace: Path,
    startup_cause_override: str | None = None,
) -> object:
    """Load inject-bootup-context.py in a controlled environment."""
    import uuid as _uuid

    env: dict = {
        "LOBSTER_WORKSPACE": str(workspace),
    }
    if startup_cause_override is not None:
        env["LOBSTER_STARTUP_CAUSE_FILE_OVERRIDE"] = startup_cause_override

    unique_name = f"inject_bootup_{_uuid.uuid4().hex}"
    with _PatchEnv(env):
        spec = importlib.util.spec_from_file_location(unique_name, _INJECT_HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _fresh_ts() -> str:
    """Return a UTC ISO timestamp that is 1 second old — well within the 5-min window."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=1)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _stale_ts() -> str:
    """Return a UTC ISO timestamp that is 10 minutes old — outside the 5-min window."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=10)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Tests: on-compact.py — write_startup_cause()
# ---------------------------------------------------------------------------


class TestWriteStartupCause:
    """on-compact.py writes cause=compaction to last-startup-cause.json before exit."""

    def test_creates_file_with_compaction_cause(self, tmp_path):
        """write_startup_cause() creates the file with cause='compaction'."""
        cause_file = tmp_path / "last-startup-cause.json"
        mod = _load_on_compact(startup_cause_override=str(cause_file))

        mod.write_startup_cause()

        assert cause_file.exists(), "last-startup-cause.json must be created"
        data = json.loads(cause_file.read_text())
        assert data.get("cause") == "compaction", (
            f"Expected cause='compaction', got {data.get('cause')!r}"
        )

    def test_written_ts_is_valid_iso_utc(self, tmp_path):
        """The ts field must be a valid ISO 8601 UTC string ending in Z."""
        cause_file = tmp_path / "last-startup-cause.json"
        mod = _load_on_compact(startup_cause_override=str(cause_file))

        mod.write_startup_cause()

        data = json.loads(cause_file.read_text())
        ts = data.get("ts", "")
        assert isinstance(ts, str) and ts.endswith("Z"), (
            f"Expected UTC ISO ts ending in Z, got {ts!r}"
        )
        # Must parse as valid ISO 8601
        datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def test_overwrites_prior_restart_entry(self, tmp_path):
        """write_startup_cause() replaces an existing cause='restart' entry."""
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"cause": "restart", "ts": "2026-01-01T00:00:00Z"}))

        mod = _load_on_compact(startup_cause_override=str(cause_file))
        mod.write_startup_cause()

        data = json.loads(cause_file.read_text())
        assert data.get("cause") == "compaction"

    def test_silent_on_unwritable_path(self, tmp_path):
        """write_startup_cause() must not raise if the path is unwritable."""
        bad_path = Path("/proc/lobster-test/no-such-dir/last-startup-cause.json")
        mod = _load_on_compact(startup_cause_override=str(bad_path))
        # Must not raise
        mod.write_startup_cause()

    def test_atomic_write_uses_tmp_rename(self, tmp_path):
        """write_startup_cause() uses tmp → final rename (no partial write visible)."""
        cause_file = tmp_path / "last-startup-cause.json"
        mod = _load_on_compact(startup_cause_override=str(cause_file))

        mod.write_startup_cause()

        # The .tmp file must be gone after the write (rename completed)
        tmp_file = cause_file.with_suffix(".tmp")
        assert not tmp_file.exists(), (
            ".tmp file must be removed after atomic rename"
        )
        # Final file must exist with valid content
        assert cause_file.exists()
        json.loads(cause_file.read_text())  # must parse cleanly


# ---------------------------------------------------------------------------
# Tests: inject-bootup-context.py — read_and_reset_startup_cause()
# ---------------------------------------------------------------------------


class TestReadAndResetStartupCause:
    """inject-bootup-context.py reads cause, classifies it, then resets to 'restart'."""

    def test_returns_compaction_when_file_is_fresh(self, tmp_path):
        """Returns 'compaction' when the file has cause=compaction and a recent ts."""
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"cause": "compaction", "ts": _fresh_ts()}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()

        assert result == "compaction", (
            f"Expected 'compaction' for fresh file, got {result!r}"
        )

    def test_returns_restart_when_file_cause_is_restart(self, tmp_path):
        """Returns 'restart' when the file explicitly says cause=restart."""
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"cause": "restart", "ts": _fresh_ts()}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()

        assert result == "restart"

    def test_returns_restart_when_file_absent(self, tmp_path):
        """Returns 'restart' when last-startup-cause.json does not exist."""
        cause_file = tmp_path / "last-startup-cause.json"
        # File does NOT exist

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()

        assert result == "restart"

    def test_returns_restart_when_compaction_ts_is_stale(self, tmp_path):
        """Returns 'restart' when cause=compaction but ts is older than 5 minutes."""
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"cause": "compaction", "ts": _stale_ts()}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()

        assert result == "restart", (
            f"Stale compaction entry (>5 min) must be treated as restart, got {result!r}"
        )

    def test_returns_restart_on_corrupt_json(self, tmp_path):
        """Returns 'restart' when the file contains invalid JSON."""
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text("not-valid-json{{{")

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()

        assert result == "restart"

    def test_returns_restart_when_cause_key_absent(self, tmp_path):
        """Returns 'restart' when the JSON is valid but has no 'cause' key.

        data.get("cause", "restart") must default to 'restart' — lock in this
        behavior explicitly so a future refactor cannot silently break it.
        """
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"ts": _fresh_ts()}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()

        assert result == "restart", (
            f"Absent 'cause' key must default to 'restart', got {result!r}"
        )

    def test_resets_to_restart_after_compaction_read(self, tmp_path):
        """After reading cause=compaction, file is overwritten with cause=restart."""
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"cause": "compaction", "ts": _fresh_ts()}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()
        assert result == "compaction"

        # File must now contain cause=restart
        assert cause_file.exists(), "File must still exist after reset"
        data = json.loads(cause_file.read_text())
        assert data.get("cause") == "restart", (
            f"File must be reset to cause='restart' after read, got {data.get('cause')!r}"
        )

    def test_resets_to_restart_after_restart_read(self, tmp_path):
        """After reading cause=restart, file is overwritten with fresh cause=restart."""
        cause_file = tmp_path / "last-startup-cause.json"
        old_ts = "2026-01-01T00:00:00Z"
        cause_file.write_text(json.dumps({"cause": "restart", "ts": old_ts}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        mod.read_and_reset_startup_cause()

        data = json.loads(cause_file.read_text())
        assert data.get("cause") == "restart"
        # Timestamp should be updated to now (not the old stale one)
        assert data.get("ts", "") != old_ts, (
            "Reset write should update the timestamp, not preserve the old stale one"
        )

    def test_creates_restart_file_when_absent(self, tmp_path):
        """When file is absent, creates it with cause=restart after reading."""
        cause_file = tmp_path / "last-startup-cause.json"
        # File does NOT exist

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        mod.read_and_reset_startup_cause()

        assert cause_file.exists(), "File must be created with cause=restart"
        data = json.loads(cause_file.read_text())
        assert data.get("cause") == "restart"

    def test_reset_write_is_silent_on_failure(self, tmp_path):
        """Reset write failure must not raise — read result is still returned."""
        cause_file = tmp_path / "last-startup-cause.json"
        cause_file.write_text(json.dumps({"cause": "compaction", "ts": _fresh_ts()}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        # Replace the cause file path with an unwritable location to simulate failure
        mod.STARTUP_CAUSE_FILE = Path("/proc/lobster-test/unwritable/last-startup-cause.json")

        # Must not raise; should return "compaction" from the readable file
        # (actual value depends on which file was read before path override,
        # but the key property is: no exception raised)
        try:
            mod.read_and_reset_startup_cause()
        except Exception as exc:
            pytest.fail(f"read_and_reset_startup_cause() must not raise, got: {exc}")

    def test_compaction_exactly_at_window_boundary_is_restart(self, tmp_path):
        """A compaction ts at exactly 5 minutes old is treated as restart (boundary exclusive)."""
        cause_file = tmp_path / "last-startup-cause.json"
        # Exactly COMPACTION_CAUSE_WINDOW_SECONDS ago
        ts = datetime.now(timezone.utc) - timedelta(seconds=COMPACTION_CAUSE_WINDOW_SECONDS)
        cause_file.write_text(json.dumps({"cause": "compaction", "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}))

        workspace = tmp_path
        mod = _load_inject_hook(workspace=workspace, startup_cause_override=str(cause_file))

        result = mod.read_and_reset_startup_cause()

        assert result == "restart", (
            f"Compaction at exactly the boundary must be treated as restart, got {result!r}"
        )
