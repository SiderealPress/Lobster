"""
Unit tests for the context-handoff.jsonl append added to dispatcher-state-stop.py
(issue #1977, redesigned in issue #2002).

The Stop hook fires whenever a dispatcher session ends — including hard context
limits and crashes that bypass the graceful wind-down LLM path. Adding a
context-handoff.jsonl append to this hook ensures the next session always has at
least minimal state to restart from. The log-append design replaces the old
write-if-absent singleton: every session end writes one record unconditionally,
building a historical record.

Behaviors verified:
1. Non-dispatcher sessions: no entry appended (skip guard works).
2. Dispatcher session: Stop hook always appends to context-handoff.jsonl.
3. Multiple dispatcher sessions: entries accumulate (log does not truncate).
4. context_pct populated from transcript JSONL when available.
5. context_pct is None when transcript is unavailable or has no usage data.
6. Required fields present: triggered_at, context_pct, in_flight_agents, note="Stop hook wind-down".
7. Hook is silent on all errors (never crashes the stop sequence).
8. Retried agents (same task_id: done then running again) appear in in_flight_agents.

Named constants (spec-derived, not reverse-engineered from implementation):
  EXPECTED_NOTE = "Stop hook wind-down"   # as specified in issue #1977
  HANDOFF_FILENAME = "context-handoff.jsonl"
"""

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Spec-derived named constants
# ---------------------------------------------------------------------------

EXPECTED_NOTE = "Stop hook wind-down"
HANDOFF_FILENAME = "context-handoff.jsonl"

# Matching the context-monitor.py constants (same spec source).
SONNET_4_6_MAX_CONTEXT = 200_000
WARNING_THRESHOLD = 70.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "dispatcher-state-stop.py"


def _load_hook():
    """Load dispatcher-state-stop.py as a fresh module.

    Inserts the hooks dir into sys.path so session_role and state_machine
    imports work. Caller is responsible for patching is_dispatcher before
    calling main().
    """
    if str(_HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOKS_DIR))
    # Also ensure src/ is on path for state_machine import.
    src_dir = _HOOKS_DIR.parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    spec = importlib.util.spec_from_file_location("dispatcher_state_stop", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_transcript(tmp_path: Path, turns: list[dict]) -> Path:
    """Write a transcript JSONL file for the given assistant turns."""
    path = tmp_path / "transcript.jsonl"
    with open(path, "w") as f:
        for turn in turns:
            obj = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": turn.get("model", "claude-sonnet-4-6"),
                    "usage": turn.get("usage", {}),
                },
            }
            f.write(json.dumps(obj) + "\n")
    return path


def _make_hook_input(session_id: str = "test-session", transcript_path: str = "") -> str:
    """Return JSON string representing hook stdin."""
    data: dict = {"session_id": session_id}
    if transcript_path:
        data["transcript_path"] = transcript_path
    return json.dumps(data)


def _run_main(mod, monkeypatch, hook_input_json: str, handoff_dir: Path) -> int:
    """Call mod.main() with patched stdin and handoff file path.

    Returns the exit code captured from sys.exit() calls.
    The handoff path is injected via LOBSTER_WORKSPACE env var pointing to
    a tmp workspace so we can inspect the written file.
    """
    # Patch stdin
    monkeypatch.setattr(sys, "stdin", StringIO(hook_input_json))
    # Point LOBSTER_WORKSPACE at a temp directory
    workspace = handoff_dir.parent
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))
    # Make data/ subdir
    (workspace / "data").mkdir(parents=True, exist_ok=True)

    exit_code = 0
    try:
        mod.main()
    except SystemExit as e:
        exit_code = e.code or 0
    return exit_code


def _read_last_entry(handoff_path: Path) -> dict:
    """Read the last JSON line from context-handoff.jsonl."""
    lines = [l.strip() for l in handoff_path.read_text().splitlines() if l.strip()]
    assert lines, f"Expected at least one entry in {handoff_path}"
    return json.loads(lines[-1])


def _read_all_entries(handoff_path: Path) -> list[dict]:
    """Read all JSON lines from context-handoff.jsonl."""
    lines = [l.strip() for l in handoff_path.read_text().splitlines() if l.strip()]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandoffNotWrittenForNonDispatcher:
    """Non-dispatcher sessions must not append to context-handoff.jsonl."""

    def test_subagent_session_writes_no_handoff(self, monkeypatch, tmp_path):
        """Stop hook for a subagent session must not append to context-handoff.jsonl."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # Load fresh module
        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: False)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: False)

        # Patch state_machine to avoid touching real state file
        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        assert not handoff_path.exists(), (
            "context-handoff.jsonl must NOT be written for subagent sessions"
        )


class TestHandoffAlwaysAppendsForDispatcher:
    """Dispatcher sessions must always append to context-handoff.jsonl."""

    def test_appends_entry_on_dispatcher_stop(self, monkeypatch, tmp_path):
        """Stop hook appends an entry to context-handoff.jsonl for dispatcher sessions."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input(session_id="disp-001")
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        assert handoff_path.exists(), "context-handoff.jsonl must be created on dispatcher stop"
        entries = _read_all_entries(handoff_path)
        assert len(entries) == 1, "Exactly one entry must be appended on first stop"

    def test_handoff_entry_contains_required_fields(self, monkeypatch, tmp_path):
        """Appended entry must contain triggered_at, context_pct, in_flight_agents, and note."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert "triggered_at" in data, "triggered_at field must be present"
        assert "context_pct" in data, "context_pct field must be present"
        assert "in_flight_agents" in data, "in_flight_agents field must be present"
        assert "note" in data, "note field must be present"

    def test_note_is_stop_hook_wind_down(self, monkeypatch, tmp_path):
        """The note field must equal 'Stop hook wind-down' exactly."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert data["note"] == EXPECTED_NOTE, (
            f"note field must be '{EXPECTED_NOTE}', got {data['note']!r}"
        )

    def test_triggered_at_is_valid_iso8601(self, monkeypatch, tmp_path):
        """triggered_at must be a parseable ISO 8601 UTC timestamp."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        ts = data["triggered_at"]
        # Should parse as ISO 8601. fromisoformat handles the +00:00 suffix.
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.year >= 2024, f"triggered_at year is implausibly old: {ts}"


class TestMultipleEntriesAccumulate:
    """Multiple session ends must accumulate entries in the log (no truncation)."""

    def test_three_stops_produce_three_entries(self, monkeypatch, tmp_path):
        """Calling the hook three times appends three entries; log is not truncated."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        for session_id in ["disp-001", "disp-002", "disp-003"]:
            mod = _load_hook()

            import session_role
            monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
            monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

            import state_machine as sm
            monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

            hook_input = _make_hook_input(session_id=session_id)
            _run_main(mod, monkeypatch, hook_input, handoff_dir)

        entries = _read_all_entries(handoff_path)
        assert len(entries) == 3, (
            f"Three session stops must produce three log entries; got {len(entries)}"
        )

    def test_last_line_is_most_recent(self, monkeypatch, tmp_path):
        """The last line in the log reflects the most recently ended session."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # Pre-seed the log with an older entry.
        old_entry = {
            "triggered_at": "2026-05-07T10:00:00+00:00",
            "context_pct": 50.0,
            "in_flight_agents": [],
            "note": EXPECTED_NOTE,
        }
        with open(handoff_path, "a") as f:
            f.write(json.dumps(old_entry) + "\n")

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input(session_id="disp-new")
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        entries = _read_all_entries(handoff_path)
        assert len(entries) == 2, "Must have two entries: seeded + new"
        # The most recent entry must not be the pre-seeded one.
        assert entries[-1]["triggered_at"] != old_entry["triggered_at"], (
            "Last entry must be the most recently appended one"
        )
        # The old entry must still be intact.
        assert entries[0]["triggered_at"] == old_entry["triggered_at"], (
            "Pre-seeded entry must not be removed"
        )


class TestContextPctFromTranscript:
    """context_pct must be populated from transcript JSONL when available."""

    def test_context_pct_populated_when_transcript_available(self, monkeypatch, tmp_path):
        """context_pct is computed from the last assistant turn's token usage."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # Build a transcript: 100k tokens out of 200k = 50%
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 80_000,
                    "cache_creation_input_tokens": 10_000,
                    "cache_read_input_tokens": 10_000,
                },
            }
        ])

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input(transcript_path=str(transcript))
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        # 100k / 200k = 50.0%
        assert data["context_pct"] == pytest.approx(50.0, abs=0.1), (
            f"context_pct should be ~50.0 for 100k/200k tokens, got {data['context_pct']}"
        )

    def test_context_pct_uses_last_turn(self, monkeypatch, tmp_path):
        """When multiple turns exist, context_pct is from the LAST assistant turn."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # First turn: 40k / 200k = 20%. Second turn: 160k / 200k = 80%.
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 40_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
            {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 160_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        ])

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input(transcript_path=str(transcript))
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert data["context_pct"] == pytest.approx(80.0, abs=0.1), (
            "context_pct must reflect the last turn, not an earlier one"
        )


class TestContextPctNullWhenUnavailable:
    """context_pct must be None when transcript is absent or has no usage data."""

    def test_context_pct_null_when_no_transcript(self, monkeypatch, tmp_path):
        """context_pct is None when no transcript_path is provided."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()  # no transcript_path
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert data["context_pct"] is None, (
            "context_pct must be None when transcript is unavailable"
        )

    def test_context_pct_null_when_transcript_missing_file(self, monkeypatch, tmp_path):
        """context_pct is None when transcript_path points to a non-existent file."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input(transcript_path="/nonexistent/path/transcript.jsonl")
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert data["context_pct"] is None, (
            "context_pct must be None when transcript file does not exist"
        )

    def test_context_pct_null_when_transcript_has_no_usage(self, monkeypatch, tmp_path):
        """context_pct is None when transcript exists but has no assistant usage blocks."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # Write a transcript with only user turns (no assistant usage).
        transcript_path = tmp_path / "empty_transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n"
        )

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input(transcript_path=str(transcript_path))
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert data["context_pct"] is None, (
            "context_pct must be None when no assistant usage data is in the transcript"
        )


def _make_inflight_jsonl(tmp_path: Path, entries: list[dict]) -> Path:
    """Write an inflight-work.jsonl file with the given entries."""
    path = tmp_path / "inflight-work.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


class TestInFlightAgents:
    """in_flight_agents must be populated from inflight-work.jsonl."""

    def test_in_flight_agents_empty_when_no_inflight_file(self, monkeypatch, tmp_path):
        """in_flight_agents is an empty list when inflight-work.jsonl does not exist."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert data["in_flight_agents"] == [], (
            "in_flight_agents must be empty when inflight-work.jsonl does not exist"
        )

    def test_in_flight_agents_contains_running_without_done(self, monkeypatch, tmp_path):
        """Running tasks without a done entry appear in in_flight_agents."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # Write inflight-work.jsonl to the workspace/data/ dir (which is handoff_dir).
        inflight_path = _make_inflight_jsonl(handoff_dir, [
            {"task_id": "task-alpha", "type": "engineering", "status": "running",
             "description": "Fix the thing", "started_at": "2026-05-07T23:00:00Z"},
        ])
        assert inflight_path.exists()

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert len(data["in_flight_agents"]) == 1, (
            "One running-without-done task must appear in in_flight_agents"
        )
        assert data["in_flight_agents"][0]["task_id"] == "task-alpha"

    def test_in_flight_agents_excludes_completed_tasks(self, monkeypatch, tmp_path):
        """Tasks with a done entry are excluded from in_flight_agents."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # task-a: running then done (completed — should be excluded)
        # task-b: only running (in-flight — should be included)
        inflight_path = _make_inflight_jsonl(handoff_dir, [
            {"task_id": "task-a", "type": "research", "status": "running",
             "description": "Investigate X", "started_at": "2026-05-07T22:00:00Z"},
            {"task_id": "task-b", "type": "engineering", "status": "running",
             "description": "Fix Y", "started_at": "2026-05-07T23:00:00Z"},
            {"task_id": "task-a", "completed_at": "2026-05-07T22:30:00Z", "status": "done"},
        ])
        assert inflight_path.exists()

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        task_ids = [a["task_id"] for a in data["in_flight_agents"]]
        assert "task-b" in task_ids, "task-b (running, no done) must be in in_flight_agents"
        assert "task-a" not in task_ids, "task-a (completed) must NOT be in in_flight_agents"

    def test_in_flight_agents_all_done_gives_empty_list(self, monkeypatch, tmp_path):
        """When all tasks are completed, in_flight_agents is an empty list."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        inflight_path = _make_inflight_jsonl(handoff_dir, [
            {"task_id": "task-done", "type": "research", "status": "running",
             "description": "All done", "started_at": "2026-05-07T22:00:00Z"},
            {"task_id": "task-done", "completed_at": "2026-05-07T22:30:00Z", "status": "done"},
        ])
        assert inflight_path.exists()

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        assert data["in_flight_agents"] == [], (
            "in_flight_agents must be empty when all tasks are done"
        )

    def test_retried_agent_appears_in_flight_after_done_then_running(self, monkeypatch, tmp_path):
        """A task that completed and was retried with the same task_id is tracked as in-flight.

        Log sequence: running -> done -> running (retry). The retry is still running when
        the session ends, so it must appear in in_flight_agents. The first done entry must
        not permanently suppress the subsequent running entry.
        """
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        # task-retry: first run completes, then retried with same task_id and still running.
        inflight_path = _make_inflight_jsonl(handoff_dir, [
            {"task_id": "task-retry", "type": "engineering", "status": "running",
             "description": "First attempt", "started_at": "2026-05-07T20:00:00Z"},
            {"task_id": "task-retry", "completed_at": "2026-05-07T20:30:00Z", "status": "done"},
            {"task_id": "task-retry", "type": "engineering", "status": "running",
             "description": "Retry attempt", "started_at": "2026-05-07T21:00:00Z"},
        ])
        assert inflight_path.exists()

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        task_ids = [a["task_id"] for a in data["in_flight_agents"]]
        assert "task-retry" in task_ids, (
            "task-retry must appear in in_flight_agents: its retry is still running"
        )

    def test_retried_agent_excluded_when_retry_also_completes(self, monkeypatch, tmp_path):
        """A retried task that also completes is excluded from in_flight_agents.

        Log sequence: running -> done -> running (retry) -> done. Both runs completed,
        so the task must not appear in in_flight_agents.
        """
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / HANDOFF_FILENAME

        inflight_path = _make_inflight_jsonl(handoff_dir, [
            {"task_id": "task-retry-done", "type": "engineering", "status": "running",
             "description": "First attempt", "started_at": "2026-05-07T20:00:00Z"},
            {"task_id": "task-retry-done", "completed_at": "2026-05-07T20:30:00Z", "status": "done"},
            {"task_id": "task-retry-done", "type": "engineering", "status": "running",
             "description": "Retry attempt", "started_at": "2026-05-07T21:00:00Z"},
            {"task_id": "task-retry-done", "completed_at": "2026-05-07T21:30:00Z", "status": "done"},
        ])
        assert inflight_path.exists()

        mod = _load_hook()

        import session_role
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, handoff_dir)

        data = _read_last_entry(handoff_path)
        task_ids = [a["task_id"] for a in data["in_flight_agents"]]
        assert "task-retry-done" not in task_ids, (
            "task-retry-done must NOT appear in in_flight_agents: retry also completed"
        )


class TestSilentOnAllErrors:
    """Hook must be silent on errors and never crash the stop sequence."""

    def test_exits_zero_when_write_fails(self, monkeypatch, tmp_path):
        """Hook exits 0 even if the handoff file append fails (read-only dir)."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()
        # Make directory read-only to force write failure.
        os.chmod(str(handoff_dir), 0o555)

        try:
            mod = _load_hook()

            import session_role
            monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: True)
            monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: True)

            import state_machine as sm
            monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

            hook_input = _make_hook_input()
            exit_code = _run_main(mod, monkeypatch, hook_input, handoff_dir)

            assert exit_code == 0, "Hook must exit 0 even when handoff append fails"
        finally:
            # Restore permissions so pytest can clean up tmp_path.
            os.chmod(str(handoff_dir), 0o755)

    def test_exits_zero_with_malformed_stdin(self, monkeypatch, tmp_path):
        """Hook exits 0 gracefully when given malformed JSON on stdin."""
        handoff_dir = tmp_path / "data"
        handoff_dir.mkdir()

        mod = _load_hook()

        import session_role
        # With malformed stdin, hook_input will be {} — is_dispatcher({}) must
        # return False, so this also tests that non-dispatcher bypass works.
        monkeypatch.setattr(session_role, "is_dispatcher", lambda _data: False)
        monkeypatch.setattr(session_role, "is_dispatcher_session", lambda _data: False)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        monkeypatch.setattr(sys, "stdin", StringIO("not valid json {{{"))
        workspace = handoff_dir.parent
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))

        exit_code = 0
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code or 0

        assert exit_code == 0, "Hook must exit 0 with malformed stdin"
