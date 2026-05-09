"""
Unit tests for the session.end event emission added to dispatcher-state-stop.py
(issue #1977, redesigned in issue #2002 to use events.jsonl via EventBus).

The Stop hook fires whenever a dispatcher session ends — including hard context
limits and crashes that bypass the graceful wind-down LLM path. Emitting a
session.end LobsterEvent to events.jsonl ensures the next session always has at
least minimal state to restart from, routed through the central EventBus.

Behaviors verified:
1. Non-dispatcher sessions (agent_id present): no entry written to events.jsonl.
2. Dispatcher session: Stop hook always emits a session.end event to events.jsonl.
3. Multiple dispatcher sessions: events accumulate in events.jsonl (log never truncated).
4. context_pct populated from transcript JSONL when available.
5. context_pct is None when transcript is unavailable or has no usage data.
6. Required fields present: event_type, severity, source, payload, timestamp, task_id, chat_id.
   Payload must contain: context_pct, in_flight_agents, note="Stop hook session end".
7. event_type must be "session.end".
8. Hook is silent on all errors (never crashes the stop sequence).
9. Retried agents (same task_id: done then running again) appear in in_flight_agents.

Named constants (spec-derived, not reverse-engineered from implementation):
  EXPECTED_NOTE = "Stop hook session end"      # payload note field
  EXPECTED_EVENT_TYPE = "session.end"          # LobsterEvent event_type
  EXPECTED_SOURCE = "dispatcher-state-stop"    # LobsterEvent source
  EVENTS_FILENAME = "events.jsonl"             # relative to logs/ in workspace
  AGENT_ID_FIELD = "agent_id"                  # field that identifies subagent payloads
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Spec-derived named constants
# ---------------------------------------------------------------------------

EXPECTED_NOTE = "Stop hook session end"
EXPECTED_EVENT_TYPE = "session.end"
EXPECTED_SOURCE = "dispatcher-state-stop"
EVENTS_FILENAME = "events.jsonl"

# The hook_input field that Claude Code injects only into subagent payloads.
# A non-empty value means subagent; absent or empty means dispatcher.
AGENT_ID_FIELD = "agent_id"
SUBAGENT_AGENT_ID = "subagent-abc-123"

# Matching the context-monitor.py constants (same spec source).
SONNET_4_6_MAX_CONTEXT = 200_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "dispatcher-state-stop.py"


def _load_hook():
    """Load dispatcher-state-stop.py as a fresh module.

    Inserts src/ into sys.path so state_machine imports work.
    """
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


def _make_hook_input(
    session_id: str = "test-session",
    transcript_path: str = "",
    agent_id: str = "",
) -> str:
    """Return JSON string representing hook stdin.

    Pass a non-empty agent_id to simulate a subagent SessionStop payload.
    Leave agent_id empty (or omit) to simulate a dispatcher SessionStop payload.
    """
    data: dict = {"session_id": session_id}
    if transcript_path:
        data["transcript_path"] = transcript_path
    if agent_id:
        data[AGENT_ID_FIELD] = agent_id
    return json.dumps(data)


def _run_main(mod, monkeypatch, hook_input_json: str, workspace: Path) -> int:
    """Call mod.main() with patched stdin and events file path.

    Returns the exit code captured from sys.exit() calls.
    The events path is injected via LOBSTER_WORKSPACE env var pointing to
    a tmp workspace so we can inspect the written file.
    """
    # Patch stdin
    monkeypatch.setattr(sys, "stdin", StringIO(hook_input_json))
    # Point LOBSTER_WORKSPACE at a temp directory
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))
    # Make logs/ and data/ subdirs
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    (workspace / "data").mkdir(parents=True, exist_ok=True)

    # Patch state_machine to avoid touching real state file
    import state_machine as sm
    monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

    exit_code = 0
    try:
        mod.main()
    except SystemExit as e:
        exit_code = e.code or 0
    return exit_code


def _events_path(workspace: Path) -> Path:
    """Return the events.jsonl path for a given workspace."""
    return workspace / "logs" / EVENTS_FILENAME


def _read_session_end_events(events_path: Path) -> list[dict]:
    """Read all session.end events from events.jsonl."""
    if not events_path.exists():
        return []
    lines = [l.strip() for l in events_path.read_text().splitlines() if l.strip()]
    events = [json.loads(line) for line in lines]
    return [e for e in events if e.get("event_type") == EXPECTED_EVENT_TYPE]


def _read_last_session_end(events_path: Path) -> dict:
    """Read the last session.end event from events.jsonl."""
    events = _read_session_end_events(events_path)
    assert events, f"Expected at least one session.end event in {events_path}"
    return events[-1]


def _read_all_events(events_path: Path) -> list[dict]:
    """Read all JSON lines from events.jsonl."""
    if not events_path.exists():
        return []
    lines = [l.strip() for l in events_path.read_text().splitlines() if l.strip()]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Tests: is_subagent() pure function
# ---------------------------------------------------------------------------


class TestIsSubagent:
    """is_subagent() must correctly classify dispatcher vs subagent hook inputs."""

    def test_subagent_when_agent_id_present(self):
        """Non-empty agent_id → subagent."""
        mod = _load_hook()
        assert mod.is_subagent({AGENT_ID_FIELD: SUBAGENT_AGENT_ID}) is True

    def test_dispatcher_when_agent_id_absent(self):
        """Missing agent_id key → dispatcher (fail-open)."""
        mod = _load_hook()
        assert mod.is_subagent({}) is False

    def test_dispatcher_when_agent_id_empty_string(self):
        """Empty string agent_id → treated as dispatcher (fail-open)."""
        mod = _load_hook()
        assert mod.is_subagent({AGENT_ID_FIELD: ""}) is False

    def test_dispatcher_when_agent_id_none(self):
        """None agent_id → treated as dispatcher (fail-open)."""
        mod = _load_hook()
        assert mod.is_subagent({AGENT_ID_FIELD: None}) is False

    def test_agent_id_field_constant_matches_spec(self):
        """AGENT_ID_FIELD constant must equal 'agent_id' (prevents silent rename drift)."""
        mod = _load_hook()
        assert mod.AGENT_ID_FIELD == "agent_id", (
            f"AGENT_ID_FIELD must be 'agent_id', got {mod.AGENT_ID_FIELD!r}"
        )


# ---------------------------------------------------------------------------
# Tests: event NOT written for non-dispatcher sessions
# ---------------------------------------------------------------------------


class TestEventNotWrittenForNonDispatcher:
    """Non-dispatcher sessions (agent_id present) must not write to events.jsonl."""

    def test_subagent_session_writes_no_event(self, monkeypatch, tmp_path):
        """Stop hook for a subagent session must not write a session.end event to events.jsonl."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()

        # Subagent session: agent_id is present and non-empty.
        hook_input = _make_hook_input(agent_id=SUBAGENT_AGENT_ID)
        _run_main(mod, monkeypatch, hook_input, workspace)

        session_end_events = _read_session_end_events(events_file)
        assert len(session_end_events) == 0, (
            "session.end must NOT be written to events.jsonl for subagent sessions"
        )

    def test_subagent_exits_zero(self, monkeypatch, tmp_path):
        """Stop hook must exit 0 for subagent sessions without any I/O."""
        mod = _load_hook()
        hook_input = _make_hook_input(agent_id=SUBAGENT_AGENT_ID)
        exit_code = _run_main(mod, monkeypatch, hook_input, tmp_path)
        assert exit_code == 0, "Hook must exit 0 for subagent sessions"


# ---------------------------------------------------------------------------
# Tests: session.end always emitted for dispatcher sessions
# ---------------------------------------------------------------------------


class TestSessionEndEventAlwaysEmittedForDispatcher:
    """Dispatcher sessions must always emit a session.end event to events.jsonl."""

    def test_emits_event_on_dispatcher_stop(self, monkeypatch, tmp_path):
        """Stop hook emits a session.end event to events.jsonl for dispatcher sessions."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()

        # Dispatcher session: no agent_id field.
        hook_input = _make_hook_input(session_id="disp-001")
        _run_main(mod, monkeypatch, hook_input, workspace)

        assert events_file.exists(), "events.jsonl must be created on dispatcher stop"
        session_end_events = _read_session_end_events(events_file)
        assert len(session_end_events) == 1, "Exactly one session.end event must be emitted on first stop"

    def test_event_has_correct_event_type(self, monkeypatch, tmp_path):
        """Emitted event must have event_type == 'session.end'."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["event_type"] == EXPECTED_EVENT_TYPE, (
            f"event_type must be '{EXPECTED_EVENT_TYPE}', got {event['event_type']!r}"
        )

    def test_event_has_lobster_event_fields(self, monkeypatch, tmp_path):
        """Emitted event must contain all LobsterEvent.to_dict() fields."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        # Top-level LobsterEvent fields (as produced by LobsterEvent.to_dict())
        for field in ("event_type", "severity", "source", "payload", "timestamp", "task_id", "chat_id"):
            assert field in event, f"LobsterEvent field '{field}' must be present in emitted event"

    def test_payload_contains_required_fields(self, monkeypatch, tmp_path):
        """Payload must contain context_pct, in_flight_agents, and note."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        payload = event["payload"]
        assert "context_pct" in payload, "payload.context_pct field must be present"
        assert "in_flight_agents" in payload, "payload.in_flight_agents field must be present"
        assert "note" in payload, "payload.note field must be present"

    def test_payload_note_is_stop_hook_session_end(self, monkeypatch, tmp_path):
        """The payload.note field must equal 'Stop hook session end' exactly."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["payload"]["note"] == EXPECTED_NOTE, (
            f"payload.note must be '{EXPECTED_NOTE}', got {event['payload']['note']!r}"
        )

    def test_event_source_is_dispatcher_state_stop(self, monkeypatch, tmp_path):
        """The source field must be 'dispatcher-state-stop'."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["source"] == EXPECTED_SOURCE, (
            f"source must be '{EXPECTED_SOURCE}', got {event['source']!r}"
        )

    def test_timestamp_is_valid_iso8601(self, monkeypatch, tmp_path):
        """timestamp must be a parseable ISO 8601 UTC timestamp."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        ts = event["timestamp"]
        # Should parse as ISO 8601. fromisoformat handles the +00:00 suffix.
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed.year >= 2024, f"timestamp year is implausibly old: {ts}"

    def test_task_id_and_chat_id_are_null(self, monkeypatch, tmp_path):
        """task_id and chat_id must be null in the emitted event."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["task_id"] is None, f"task_id must be null, got {event['task_id']!r}"
        assert event["chat_id"] is None, f"chat_id must be null, got {event['chat_id']!r}"

    def test_fail_open_empty_agent_id_treated_as_dispatcher(self, monkeypatch, tmp_path):
        """agent_id='' (empty string) is treated as dispatcher — fail-open."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        # Pass empty string agent_id — must be treated as dispatcher.
        hook_input = json.dumps({AGENT_ID_FIELD: "", "session_id": "disp-session"})
        _run_main(mod, monkeypatch, hook_input, workspace)

        session_end_events = _read_session_end_events(events_file)
        assert len(session_end_events) == 1, (
            "Empty agent_id must be treated as dispatcher — session.end must be emitted"
        )


# ---------------------------------------------------------------------------
# Tests: multiple entries accumulate (no truncation)
# ---------------------------------------------------------------------------


class TestMultipleEntriesAccumulate:
    """Multiple session ends must accumulate events in events.jsonl (no truncation)."""

    def test_three_stops_produce_three_session_end_events(self, monkeypatch, tmp_path):
        """Calling the hook three times appends three session.end events; log not truncated."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        for session_id in ["disp-001", "disp-002", "disp-003"]:
            mod = _load_hook()
            hook_input = _make_hook_input(session_id=session_id)
            _run_main(mod, monkeypatch, hook_input, workspace)

        session_end_events = _read_session_end_events(events_file)
        assert len(session_end_events) == 3, (
            f"Three session stops must produce three session.end events; got {len(session_end_events)}"
        )

    def test_last_event_is_most_recent(self, monkeypatch, tmp_path):
        """The last session.end event reflects the most recently ended session."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        # Ensure logs/ dir exists before seeding events.jsonl.
        events_file.parent.mkdir(parents=True, exist_ok=True)

        # Pre-seed events.jsonl with an older session.end event.
        old_event = {
            "event_type": "session.end",
            "severity": "info",
            "source": "dispatcher-state-stop",
            "payload": {"context_pct": 50.0, "in_flight_agents": [], "note": EXPECTED_NOTE},
            "timestamp": "2026-05-07T10:00:00+00:00",
            "task_id": None,
            "chat_id": None,
        }
        with open(events_file, "a") as f:
            f.write(json.dumps(old_event) + "\n")

        mod = _load_hook()
        hook_input = _make_hook_input(session_id="disp-new")
        _run_main(mod, monkeypatch, hook_input, workspace)

        session_end_events = _read_session_end_events(events_file)
        assert len(session_end_events) == 2, "Must have two session.end events: seeded + new"
        # The most recent event must not be the pre-seeded one.
        assert session_end_events[-1]["timestamp"] != old_event["timestamp"], (
            "Last session.end event must be the most recently appended one"
        )
        # The old event must still be intact.
        assert session_end_events[0]["timestamp"] == old_event["timestamp"], (
            "Pre-seeded event must not be removed"
        )


# ---------------------------------------------------------------------------
# Tests: context_pct from transcript
# ---------------------------------------------------------------------------


class TestContextPctFromTranscript:
    """context_pct must be populated from transcript JSONL when available."""

    def test_context_pct_populated_when_transcript_available(self, monkeypatch, tmp_path):
        """context_pct is computed from the last assistant turn's token usage."""
        workspace = tmp_path
        events_file = _events_path(workspace)

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
        hook_input = _make_hook_input(transcript_path=str(transcript))
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        # 100k / 200k = 50.0%
        assert event["payload"]["context_pct"] == pytest.approx(50.0, abs=0.1), (
            f"context_pct should be ~50.0 for 100k/200k tokens, got {event['payload']['context_pct']}"
        )

    def test_context_pct_uses_last_turn(self, monkeypatch, tmp_path):
        """When multiple turns exist, context_pct is from the LAST assistant turn."""
        workspace = tmp_path
        events_file = _events_path(workspace)

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
        hook_input = _make_hook_input(transcript_path=str(transcript))
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["payload"]["context_pct"] == pytest.approx(80.0, abs=0.1), (
            "context_pct must reflect the last turn, not an earlier one"
        )


# ---------------------------------------------------------------------------
# Tests: context_pct is None when unavailable
# ---------------------------------------------------------------------------


class TestContextPctNullWhenUnavailable:
    """context_pct must be None when transcript is absent or has no usage data."""

    def test_context_pct_null_when_no_transcript(self, monkeypatch, tmp_path):
        """context_pct is None when no transcript_path is provided."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()  # no transcript_path
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["payload"]["context_pct"] is None, (
            "context_pct must be None when transcript is unavailable"
        )

    def test_context_pct_null_when_transcript_missing_file(self, monkeypatch, tmp_path):
        """context_pct is None when transcript_path points to a non-existent file."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input(transcript_path="/nonexistent/path/transcript.jsonl")
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["payload"]["context_pct"] is None, (
            "context_pct must be None when transcript file does not exist"
        )

    def test_context_pct_null_when_transcript_has_no_usage(self, monkeypatch, tmp_path):
        """context_pct is None when transcript exists but has no assistant usage blocks."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        # Write a transcript with only user turns (no assistant usage).
        transcript_path = tmp_path / "empty_transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n"
        )

        mod = _load_hook()
        hook_input = _make_hook_input(transcript_path=str(transcript_path))
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["payload"]["context_pct"] is None, (
            "context_pct must be None when no assistant usage data is in the transcript"
        )


# ---------------------------------------------------------------------------
# Helpers for inflight tests
# ---------------------------------------------------------------------------


def _make_inflight_jsonl(data_dir: Path, entries: list[dict]) -> Path:
    """Write an inflight-work.jsonl file with the given entries."""
    path = data_dir / "inflight-work.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


# ---------------------------------------------------------------------------
# Tests: in_flight_agents from inflight-work.jsonl
# ---------------------------------------------------------------------------


class TestInFlightAgents:
    """in_flight_agents must be populated from inflight-work.jsonl."""

    def test_in_flight_agents_empty_when_no_inflight_file(self, monkeypatch, tmp_path):
        """in_flight_agents is an empty list when inflight-work.jsonl does not exist."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["payload"]["in_flight_agents"] == [], (
            "in_flight_agents must be empty when inflight-work.jsonl does not exist"
        )

    def test_in_flight_agents_contains_running_without_done(self, monkeypatch, tmp_path):
        """Running tasks without a done entry appear in in_flight_agents."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        # Write inflight-work.jsonl to the workspace/data/ dir.
        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _make_inflight_jsonl(data_dir, [
            {"task_id": "task-alpha", "type": "engineering", "status": "running",
             "description": "Fix the thing", "started_at": "2026-05-07T23:00:00Z"},
        ])

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert len(event["payload"]["in_flight_agents"]) == 1, (
            "One running-without-done task must appear in in_flight_agents"
        )
        assert event["payload"]["in_flight_agents"][0]["task_id"] == "task-alpha"

    def test_in_flight_agents_excludes_completed_tasks(self, monkeypatch, tmp_path):
        """Tasks with a done entry are excluded from in_flight_agents."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        # task-a: running then done (completed — should be excluded)
        # task-b: only running (in-flight — should be included)
        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _make_inflight_jsonl(data_dir, [
            {"task_id": "task-a", "type": "research", "status": "running",
             "description": "Investigate X", "started_at": "2026-05-07T22:00:00Z"},
            {"task_id": "task-b", "type": "engineering", "status": "running",
             "description": "Fix Y", "started_at": "2026-05-07T23:00:00Z"},
            {"task_id": "task-a", "completed_at": "2026-05-07T22:30:00Z", "status": "done"},
        ])

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        task_ids = [a["task_id"] for a in event["payload"]["in_flight_agents"]]
        assert "task-b" in task_ids, "task-b (running, no done) must be in in_flight_agents"
        assert "task-a" not in task_ids, "task-a (completed) must NOT be in in_flight_agents"

    def test_in_flight_agents_all_done_gives_empty_list(self, monkeypatch, tmp_path):
        """When all tasks are completed, in_flight_agents is an empty list."""
        workspace = tmp_path
        events_file = _events_path(workspace)

        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _make_inflight_jsonl(data_dir, [
            {"task_id": "task-done", "type": "research", "status": "running",
             "description": "All done", "started_at": "2026-05-07T22:00:00Z"},
            {"task_id": "task-done", "completed_at": "2026-05-07T22:30:00Z", "status": "done"},
        ])

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        assert event["payload"]["in_flight_agents"] == [], (
            "in_flight_agents must be empty when all tasks are done"
        )

    def test_retried_agent_appears_in_flight_after_done_then_running(self, monkeypatch, tmp_path):
        """A task that completed and was retried with the same task_id is tracked as in-flight.

        Log sequence: running -> done -> running (retry). The retry is still running when
        the session ends, so it must appear in in_flight_agents. The first done entry must
        not permanently suppress the subsequent running entry.
        """
        workspace = tmp_path
        events_file = _events_path(workspace)

        # task-retry: first run completes, then retried with same task_id and still running.
        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _make_inflight_jsonl(data_dir, [
            {"task_id": "task-retry", "type": "engineering", "status": "running",
             "description": "First attempt", "started_at": "2026-05-07T20:00:00Z"},
            {"task_id": "task-retry", "completed_at": "2026-05-07T20:30:00Z", "status": "done"},
            {"task_id": "task-retry", "type": "engineering", "status": "running",
             "description": "Retry attempt", "started_at": "2026-05-07T21:00:00Z"},
        ])

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        task_ids = [a["task_id"] for a in event["payload"]["in_flight_agents"]]
        assert "task-retry" in task_ids, (
            "task-retry must appear in in_flight_agents: its retry is still running"
        )

    def test_retried_agent_excluded_when_retry_also_completes(self, monkeypatch, tmp_path):
        """A retried task that also completes is excluded from in_flight_agents.

        Log sequence: running -> done -> running (retry) -> done. Both runs completed,
        so the task must not appear in in_flight_agents.
        """
        workspace = tmp_path
        events_file = _events_path(workspace)

        data_dir = workspace / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _make_inflight_jsonl(data_dir, [
            {"task_id": "task-retry-done", "type": "engineering", "status": "running",
             "description": "First attempt", "started_at": "2026-05-07T20:00:00Z"},
            {"task_id": "task-retry-done", "completed_at": "2026-05-07T20:30:00Z", "status": "done"},
            {"task_id": "task-retry-done", "type": "engineering", "status": "running",
             "description": "Retry attempt", "started_at": "2026-05-07T21:00:00Z"},
            {"task_id": "task-retry-done", "completed_at": "2026-05-07T21:30:00Z", "status": "done"},
        ])

        mod = _load_hook()
        hook_input = _make_hook_input()
        _run_main(mod, monkeypatch, hook_input, workspace)

        event = _read_last_session_end(events_file)
        task_ids = [a["task_id"] for a in event["payload"]["in_flight_agents"]]
        assert "task-retry-done" not in task_ids, (
            "task-retry-done must NOT appear in in_flight_agents: retry also completed"
        )


# ---------------------------------------------------------------------------
# Tests: hook is silent on all errors
# ---------------------------------------------------------------------------


class TestSilentOnAllErrors:
    """Hook must be silent on errors and never crash the stop sequence."""

    def test_exits_zero_when_write_fails(self, monkeypatch, tmp_path):
        """Hook exits 0 even if the events.jsonl write fails (read-only dir)."""
        workspace = tmp_path
        logs_dir = workspace / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        # Make logs/ directory read-only to force write failure.
        os.chmod(str(logs_dir), 0o555)

        try:
            mod = _load_hook()
            hook_input = _make_hook_input()
            exit_code = _run_main(mod, monkeypatch, hook_input, workspace)

            assert exit_code == 0, "Hook must exit 0 even when events.jsonl write fails"
        finally:
            # Restore permissions so pytest can clean up tmp_path.
            os.chmod(str(logs_dir), 0o755)

    def test_exits_zero_with_malformed_stdin(self, monkeypatch, tmp_path):
        """Hook exits 0 gracefully when given malformed JSON on stdin — treated as dispatcher (fail-open)."""
        workspace = tmp_path

        mod = _load_hook()

        monkeypatch.setattr(sys, "stdin", StringIO("not valid json {{{"))
        monkeypatch.setenv("LOBSTER_WORKSPACE", str(workspace))
        (workspace / "logs").mkdir(parents=True, exist_ok=True)
        (workspace / "data").mkdir(parents=True, exist_ok=True)

        import state_machine as sm
        monkeypatch.setattr(sm, "write_state", lambda *a, **kw: None)

        exit_code = 0
        try:
            mod.main()
        except SystemExit as e:
            exit_code = e.code or 0

        assert exit_code == 0, "Hook must exit 0 with malformed stdin"
