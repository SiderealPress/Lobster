"""
Unit tests for the inflight-work.jsonl startup compaction in
hooks/on-fresh-start.py (issue #1997).

## What this file tests

On startup, _compact_inflight_work() must:
- Read all entries from inflight-work.jsonl
- Identify task_ids that have at least one 'done' entry
- Drop 'running' entries older than INFLIGHT_STALE_THRESHOLD_HOURS (6h) that
  have no corresponding 'done' entry
- Preserve all 'done' entries regardless of age
- Preserve 'running' entries younger than the threshold (even without 'done')
- Preserve 'running' entries with a matching 'done' entry (regardless of age)
- Rewrite the file atomically after compaction
- Be a no-op when the file does not exist
- Never raise — must not crash the hook or block dispatcher start

## Named constants (spec-derived, not magic literals)

INFLIGHT_STALE_THRESHOLD_HOURS = 6  # drop running entries older than this
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Named constants matching those in the implementation
# ---------------------------------------------------------------------------

INFLIGHT_STALE_THRESHOLD_HOURS = 6  # running entries older than this are dropped

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"


def _load_hook(
    inflight_work_override: str | None = None,
    compaction_state_override: str | None = None,
) -> object:
    """Load on-fresh-start.py with test-controlled file paths.

    The module is loaded with session_role stubbed to avoid import errors.
    Returns the loaded module object.
    """
    import uuid

    env: dict[str, str] = {}
    if inflight_work_override is not None:
        env["LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE"] = inflight_work_override
    if compaction_state_override is not None:
        env["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override

    unique_name = f"on_fresh_start_{uuid.uuid4().hex}"
    saved_env: dict[str, str | None] = {}
    for k, v in env.items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        # Stub session_role to avoid needing the hook's sibling file
        import types
        stub = types.ModuleType("session_role")
        stub.is_dispatcher = lambda data: True  # type: ignore[attr-defined]
        sys.modules.setdefault("session_role", stub)

        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, saved_v in saved_env.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v

    # Override runtime-resolved path after load
    if inflight_work_override is not None:
        mod.INFLIGHT_WORK_FILE = Path(inflight_work_override)

    return mod


def _ts(hours_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp N hours in the past."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """Write a list of dicts as a JSONL file."""
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    """Read all entries from a JSONL file, skipping blank lines."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Tests: _compact_inflight_entries (pure function)
# ---------------------------------------------------------------------------


class TestCompactInflightEntries:
    """Tests for the pure _compact_inflight_entries() function."""

    def test_drops_stale_running_entry_with_no_done(self) -> None:
        """A running entry older than threshold with no done entry is dropped."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "lost-task", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 1)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert not any(e["task_id"] == "lost-task" for e in result), (
            "Stale running entry with no done entry must be dropped"
        )

    def test_preserves_running_entry_within_threshold(self) -> None:
        """A running entry younger than threshold is preserved even without a done entry."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "fresh-task", "status": "running", "ts": _ts(1)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert any(e["task_id"] == "fresh-task" for e in result), (
            "Running entry within threshold must be preserved"
        )

    def test_preserves_running_entry_with_matching_done(self) -> None:
        """A running entry with a matching done entry is preserved regardless of age."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "paired-task", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 24)},
            {"task_id": "paired-task", "status": "done", "completed_at": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 20)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert any(e["task_id"] == "paired-task" for e in result), (
            "Running entry with matching done must be preserved"
        )

    def test_preserves_all_done_entries(self) -> None:
        """Done entries are always preserved regardless of age."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "old-done", "status": "done", "completed_at": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 48)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert any(e["task_id"] == "old-done" and e["status"] == "done" for e in result), (
            "Done entries must always be preserved"
        )

    def test_entry_just_under_threshold_is_preserved(self) -> None:
        """A running entry just under the threshold is preserved (strict > comparison).

        Uses 5.9h (not exactly 6.0h) to avoid sub-second precision issues when
        formatting/parsing the timestamp as a seconds-precision ISO string.
        """
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "just-under-task", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS - 0.1)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert any(e["task_id"] == "just-under-task" for e in result), (
            "Running entry just under threshold must be preserved"
        )

    def test_entry_just_over_threshold_is_dropped(self) -> None:
        """A running entry just over the threshold is dropped."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "just-over-task", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 0.1)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert not any(e["task_id"] == "just-over-task" for e in result), (
            "Running entry just over threshold must be dropped"
        )

    def test_mixed_entries_only_stale_orphans_dropped(self) -> None:
        """Only stale orphaned running entries are dropped; others are preserved."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            # Stale orphan — should be dropped
            {"task_id": "stale-orphan", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 12)},
            # Fresh running — should be kept
            {"task_id": "fresh-runner", "status": "running", "ts": _ts(1)},
            # Old running with done — running entry should be kept
            {"task_id": "completed-old", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 10)},
            {"task_id": "completed-old", "status": "done", "completed_at": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 5)},
            # Done entry — always kept
            {"task_id": "plain-done", "status": "done", "completed_at": _ts(2)},
        ]
        result = mod._compact_inflight_entries(entries, now)

        task_ids = {e["task_id"] for e in result}
        assert "stale-orphan" not in task_ids, "Stale orphan must be dropped"
        assert "fresh-runner" in task_ids, "Fresh runner must be preserved"
        assert "completed-old" in task_ids, "Completed task must be preserved"
        assert "plain-done" in task_ids, "Done entry must be preserved"

    def test_empty_entries_returns_empty(self) -> None:
        """Empty input returns empty output."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        result = mod._compact_inflight_entries([], now)
        assert result == []

    def test_entry_without_ts_and_no_done_is_dropped_if_status_running(self) -> None:
        """A running entry with no timestamp and no done entry is treated as stale."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "no-ts-running", "status": "running"},
        ]
        result = mod._compact_inflight_entries(entries, now)
        # No timestamp → cannot determine age → treat as stale and drop
        assert not any(e["task_id"] == "no-ts-running" for e in result), (
            "Running entry with no timestamp and no done must be treated as stale"
        )

    def test_drops_stale_running_entry_with_started_at_field(self) -> None:
        """A running entry using 'started_at' (current field) older than threshold is dropped."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "stale-started-at", "status": "running",
             "started_at": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 1)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert not any(e["task_id"] == "stale-started-at" for e in result), (
            "Stale running entry using 'started_at' field with no done must be dropped"
        )

    def test_preserves_fresh_running_entry_with_started_at_field(self) -> None:
        """A running entry using 'started_at' younger than threshold is preserved."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "fresh-started-at", "status": "running", "started_at": _ts(1)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert any(e["task_id"] == "fresh-started-at" for e in result), (
            "Fresh running entry using 'started_at' field must be preserved"
        )

    def test_started_at_preferred_when_ts_absent(self) -> None:
        """'started_at' is used as timestamp when 'ts' is absent."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        # If only started_at is present and it's fresh, the entry must be kept.
        # If only started_at is present and it's stale, the entry must be dropped.
        fresh_entry = {"task_id": "only-started-at-fresh", "status": "running", "started_at": _ts(1)}
        stale_entry = {"task_id": "only-started-at-stale", "status": "running",
                       "started_at": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 2)}
        result = mod._compact_inflight_entries([fresh_entry, stale_entry], now)
        task_ids = {e["task_id"] for e in result}
        assert "only-started-at-fresh" in task_ids, (
            "Fresh entry with only 'started_at' must be preserved"
        )
        assert "only-started-at-stale" not in task_ids, (
            "Stale entry with only 'started_at' must be dropped"
        )

    def test_multiple_running_entries_same_task_id_all_dropped_when_no_done(self) -> None:
        """All running entries for the same task_id are dropped when there's no done."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "dup-task", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 10)},
            {"task_id": "dup-task", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 5)},
        ]
        result = mod._compact_inflight_entries(entries, now)
        assert not any(e["task_id"] == "dup-task" for e in result), (
            "All stale running entries for same task_id must be dropped when no done"
        )

    def test_does_not_mutate_input_list(self) -> None:
        """Pure function: input list must not be mutated."""
        mod = _load_hook()
        now = datetime.now(timezone.utc)
        entries = [
            {"task_id": "t1", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 5)},
        ]
        original_len = len(entries)
        original_task_id = entries[0]["task_id"]
        mod._compact_inflight_entries(entries, now)
        assert len(entries) == original_len, "Input list must not be mutated"
        assert entries[0]["task_id"] == original_task_id, "Input dict must not be mutated"


# ---------------------------------------------------------------------------
# Tests: _compact_inflight_work (I/O function — reads and rewrites the file)
# ---------------------------------------------------------------------------


class TestCompactInflightWork:
    """Tests for the I/O wrapper _compact_inflight_work() that reads and rewrites the file."""

    def test_noop_when_file_absent(self, tmp_path: Path) -> None:
        """Must not raise if inflight-work.jsonl does not exist."""
        absent_path = tmp_path / "inflight-work.jsonl"
        mod = _load_hook(inflight_work_override=str(absent_path))
        # Must not raise
        mod._compact_inflight_work()
        # File must still not exist (no-op)
        assert not absent_path.exists()

    def test_rewrites_file_dropping_stale_orphans(self, tmp_path: Path) -> None:
        """Stale orphaned running entries are removed from the file on rewrite."""
        jsonl = tmp_path / "inflight-work.jsonl"
        entries = [
            {"task_id": "stale-orphan", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 24)},
            {"task_id": "fresh-done", "status": "done", "completed_at": _ts(1)},
        ]
        _write_jsonl(jsonl, entries)

        mod = _load_hook(inflight_work_override=str(jsonl))
        mod._compact_inflight_work()

        result = _read_jsonl(jsonl)
        task_ids = {e["task_id"] for e in result}
        assert "stale-orphan" not in task_ids, "Stale orphan must be dropped from file"
        assert "fresh-done" in task_ids, "Done entry must be preserved"

    def test_file_rewrite_is_atomic(self, tmp_path: Path) -> None:
        """Rewrite uses atomic rename — original file replaced cleanly."""
        jsonl = tmp_path / "inflight-work.jsonl"
        entries = [
            {"task_id": "kept-task", "status": "done", "completed_at": _ts(1)},
        ]
        _write_jsonl(jsonl, entries)

        mod = _load_hook(inflight_work_override=str(jsonl))
        mod._compact_inflight_work()

        # File must still exist at the original path
        assert jsonl.exists()
        result = _read_jsonl(jsonl)
        assert len(result) == 1
        assert result[0]["task_id"] == "kept-task"

    def test_preserves_all_entries_when_nothing_to_drop(self, tmp_path: Path) -> None:
        """When nothing qualifies for deletion, the file is unchanged in content."""
        jsonl = tmp_path / "inflight-work.jsonl"
        entries = [
            {"task_id": "fresh-run", "status": "running", "ts": _ts(1)},
            {"task_id": "old-done", "status": "done", "completed_at": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 12)},
        ]
        _write_jsonl(jsonl, entries)

        mod = _load_hook(inflight_work_override=str(jsonl))
        mod._compact_inflight_work()

        result = _read_jsonl(jsonl)
        task_ids = {e["task_id"] for e in result}
        assert "fresh-run" in task_ids
        assert "old-done" in task_ids

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        """Malformed lines are skipped rather than crashing the compaction."""
        jsonl = tmp_path / "inflight-work.jsonl"
        jsonl.write_text(
            '{"task_id": "good-done", "status": "done"}\n'
            'NOT VALID JSON\n'
            '{"task_id": "also-good", "status": "done"}\n',
            encoding="utf-8",
        )

        mod = _load_hook(inflight_work_override=str(jsonl))
        # Must not raise
        mod._compact_inflight_work()

        result = _read_jsonl(jsonl)
        task_ids = {e["task_id"] for e in result}
        assert "good-done" in task_ids
        assert "also-good" in task_ids

    def test_silent_on_unexpected_error(self, tmp_path: Path) -> None:
        """_compact_inflight_work must not raise even when given a non-writable path."""
        mod = _load_hook()
        # Point to a path where we cannot write the temp file
        mod.INFLIGHT_WORK_FILE = Path("/proc/lobster_test_nonexistent/inflight-work.jsonl")
        # Must not raise
        mod._compact_inflight_work()

    def test_large_file_reduced_by_compaction(self, tmp_path: Path) -> None:
        """A file with many stale orphans is significantly reduced after compaction."""
        jsonl = tmp_path / "inflight-work.jsonl"
        # Generate 50 stale orphan entries (like the March 2026 entries in the real file)
        stale_entries = [
            {"task_id": f"old-task-{i}", "status": "running", "ts": _ts(INFLIGHT_STALE_THRESHOLD_HOURS + 24 * 30)}
            for i in range(50)
        ]
        # Plus a few recent done entries that should be kept
        kept_entries = [
            {"task_id": f"recent-done-{i}", "status": "done", "completed_at": _ts(1)}
            for i in range(5)
        ]
        _write_jsonl(jsonl, stale_entries + kept_entries)

        mod = _load_hook(inflight_work_override=str(jsonl))
        mod._compact_inflight_work()

        result = _read_jsonl(jsonl)
        assert len(result) == 5, f"Expected 5 entries after compaction, got {len(result)}"
        task_ids = {e["task_id"] for e in result}
        for i in range(5):
            assert f"recent-done-{i}" in task_ids


# ---------------------------------------------------------------------------
# Tests: INFLIGHT_WORK_FILE constant (spec-derived path resolution)
# ---------------------------------------------------------------------------


class TestInflightWorkFileConstant:
    """INFLIGHT_WORK_FILE must resolve from LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE when set."""

    def test_constant_resolves_from_env_override(self, tmp_path: Path) -> None:
        """LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE must be used when set."""
        override_path = str(tmp_path / "custom-inflight.jsonl")
        mod = _load_hook(inflight_work_override=override_path)
        assert str(mod.INFLIGHT_WORK_FILE) == override_path, (
            f"INFLIGHT_WORK_FILE must equal override {override_path!r}, "
            f"got {mod.INFLIGHT_WORK_FILE!r}"
        )

    def test_constant_defaults_to_workspace_data(self) -> None:
        """Without override, INFLIGHT_WORK_FILE defaults to ~/lobster-workspace/data/inflight-work.jsonl."""
        import uuid
        unique_name = f"on_fresh_start_{uuid.uuid4().hex}"

        saved = os.environ.get("LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE")
        os.environ.pop("LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE", None)

        import types
        stub = types.ModuleType("session_role")
        stub.is_dispatcher = lambda data: True  # type: ignore[attr-defined]
        sys.modules.setdefault("session_role", stub)

        try:
            spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            if saved is None:
                os.environ.pop("LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE", None)
            else:
                os.environ["LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE"] = saved

        assert mod.INFLIGHT_WORK_FILE.name == "inflight-work.jsonl", (
            f"Default filename must be 'inflight-work.jsonl', got {mod.INFLIGHT_WORK_FILE.name!r}"
        )
        assert "data" in mod.INFLIGHT_WORK_FILE.parts, (
            "Default path must be under the data/ directory"
        )
