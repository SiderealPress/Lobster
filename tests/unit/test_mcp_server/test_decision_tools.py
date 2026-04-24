"""
Tests for write_decision and list_decisions MCP tools.

Behavior being tested:
- write_decision stores a structured decision in memory with type='decision'
- write_decision validates required fields and rejects incomplete inputs
- write_decision builds canonical content that encodes all structured fields
- write_decision records supersession metadata when supersedes is provided
- list_decisions retrieves only decision-type events
- list_decisions filters out superseded decisions when active_only=True
- list_decisions filters by area when area parameter is provided
- list_decisions returns a clear message when no decisions exist
- list_decisions returns all decisions (including superseded) when active_only=False
"""

import asyncio
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# Named constants matching spec requirements
DECISION_TYPE = "decision"
REQUIRED_FIELDS = {"key", "title", "decision", "rationale", "date"}


class TestWriteDecisionValidation:
    """Tests for write_decision input validation."""

    def test_rejects_missing_key(self):
        """write_decision must reject requests missing the key field."""
        from src.mcp.inbox_server import handle_write_decision

        args = {
            "title": "Some Decision",
            "decision": "Do the thing",
            "rationale": "Because",
            "date": "2026-04",
        }
        # Missing 'key'
        result = asyncio.run(handle_write_decision(args))
        assert "missing" in result[0].text.lower() or "error" in result[0].text.lower()
        assert "key" in result[0].text

    def test_rejects_missing_rationale(self):
        """write_decision must reject requests missing rationale — decisions without rationale are just rules."""
        from src.mcp.inbox_server import handle_write_decision

        args = {
            "key": "test-decision",
            "title": "Some Decision",
            "decision": "Do the thing",
            "date": "2026-04",
            # Missing 'rationale'
        }
        result = asyncio.run(handle_write_decision(args))
        assert "missing" in result[0].text.lower() or "error" in result[0].text.lower()
        assert "rationale" in result[0].text

    def test_rejects_empty_key(self):
        """write_decision must reject an empty key string."""
        from src.mcp.inbox_server import handle_write_decision

        mock_provider = MagicMock()
        with patch("src.mcp.inbox_server._memory_provider", mock_provider):
            args = {
                "key": "   ",  # whitespace only
                "title": "Some Decision",
                "decision": "Do the thing",
                "rationale": "Because it is right",
                "date": "2026-04",
            }
            result = asyncio.run(handle_write_decision(args))
        assert "error" in result[0].text.lower()
        assert "key" in result[0].text.lower()

    def test_rejects_when_memory_unavailable(self):
        """write_decision must return an error when the memory provider is None."""
        from src.mcp.inbox_server import handle_write_decision

        with patch("src.mcp.inbox_server._memory_provider", None):
            args = {
                "key": "test-decision",
                "title": "Some Decision",
                "decision": "Do the thing",
                "rationale": "Because it is right",
                "date": "2026-04",
            }
            result = asyncio.run(handle_write_decision(args))
        assert "not available" in result[0].text.lower()


class TestWriteDecisionStorage:
    """Tests for write_decision storage behavior."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp(prefix="lobster_test_decision_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def static_provider(self, temp_dir):
        from src.mcp.memory.static_memory import StaticMemory
        return StaticMemory(
            canonical_dir=temp_dir / "canonical",
            event_log=temp_dir / "events.jsonl",
        )

    def test_stores_event_with_decision_type(self, static_provider):
        """write_decision stores an event with type='decision'."""
        from src.mcp.inbox_server import handle_write_decision

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            args = {
                "key": "relay-pattern-deprecated",
                "title": "Relay pattern deprecated",
                "decision": "Subagents must call send_reply directly.",
                "rationale": "Relay caused duplicates on restart.",
                "date": "2026-04",
                "affected_areas": ["subagent-communication", "write_result"],
            }
            result = asyncio.run(handle_write_decision(args))

        assert "error" not in result[0].text.lower()
        assert "stored" in result[0].text.lower() or "decision" in result[0].text.lower()

        # Verify the stored event type
        stored = static_provider.search("relay-pattern-deprecated")
        assert len(stored) >= 1
        decision_events = [e for e in stored if e.type == DECISION_TYPE]
        assert len(decision_events) >= 1

    def test_content_encodes_all_required_fields(self, static_provider):
        """The stored content string must encode all structured fields for searchability."""
        from src.mcp.inbox_server import handle_write_decision

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            args = {
                "key": "event-bus-transport-nats",
                "title": "NATS chosen as event bus transport",
                "decision": "Use NATS for all inter-service messaging.",
                "rationale": "Lower latency than Redis pub/sub in our benchmarks.",
                "date": "2026-03",
                "affected_areas": ["event-bus", "inter-service"],
            }
            asyncio.run(handle_write_decision(args))

        results = static_provider.search("event-bus-transport-nats")
        assert len(results) >= 1
        event = next(e for e in results if e.type == DECISION_TYPE)

        # All key fields must be discoverable in the content
        assert "event-bus-transport-nats" in event.content
        assert "NATS chosen as event bus transport" in event.content
        assert "Lower latency" in event.content  # rationale
        assert "2026-03" in event.content  # date
        assert "event-bus" in event.content  # affected_areas

    def test_metadata_contains_structured_fields(self, static_provider):
        """write_decision stores structured metadata for programmatic access."""
        from src.mcp.inbox_server import handle_write_decision

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            args = {
                "key": "direct-delivery-canonical",
                "title": "Direct delivery is canonical",
                "decision": "Always use send_reply then write_result(sent_reply_to_user=True).",
                "rationale": "Crash-safe delivery.",
                "date": "2026-04",
                "affected_areas": ["subagent-communication"],
            }
            asyncio.run(handle_write_decision(args))

        results = static_provider.search("direct-delivery-canonical")
        assert len(results) >= 1
        event = next(e for e in results if e.type == DECISION_TYPE)

        assert event.metadata.get("decision_key") == "direct-delivery-canonical"
        assert event.metadata.get("decision_title") == "Direct delivery is canonical"
        assert event.metadata.get("decision_date") == "2026-04"
        assert "subagent-communication" in event.metadata.get("decision_affected_areas", [])
        assert "decision" in event.metadata.get("tags", [])
        assert "architecture" in event.metadata.get("tags", [])

    def test_supersedes_recorded_in_metadata(self, static_provider):
        """When supersedes is provided, it must be recorded in the new decision's metadata."""
        from src.mcp.inbox_server import handle_write_decision

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            # Store original decision
            asyncio.run(handle_write_decision({
                "key": "old-pattern",
                "title": "Old pattern",
                "decision": "Use X.",
                "rationale": "Made sense at the time.",
                "date": "2025-01",
            }))

            # Store superseding decision
            asyncio.run(handle_write_decision({
                "key": "new-pattern",
                "title": "New pattern",
                "decision": "Use Y instead of X.",
                "rationale": "Y is better because Z.",
                "date": "2026-04",
                "supersedes": "old-pattern",
            }))

        results = static_provider.search("new-pattern")
        new_event = next(e for e in results if e.type == DECISION_TYPE and "new-pattern" in e.content)
        assert new_event.metadata.get("supersedes") == "old-pattern"
        assert "old-pattern" in new_event.content  # also encoded in content

    def test_returns_success_with_event_id(self, static_provider):
        """write_decision returns a success message including the event ID."""
        from src.mcp.inbox_server import handle_write_decision

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            args = {
                "key": "test-key",
                "title": "Test",
                "decision": "Do X.",
                "rationale": "Y.",
                "date": "2026-04",
            }
            result = asyncio.run(handle_write_decision(args))

        text = result[0].text
        assert "error" not in text.lower()
        # Should mention the event was stored
        assert "stored" in text.lower() or "decision" in text.lower()
        # Should contain the key
        assert "test-key" in text

    def test_write_decision_supersedes_nonexistent_key_accepted_silently(self, static_provider):
        """write_decision with supersedes pointing to a nonexistent key must succeed silently.

        The decision is stored normally. list_decisions must return it.
        No error or crash should occur — nonexistent supersedes keys are silently ignored.
        """
        from src.mcp.inbox_server import handle_write_decision, handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            result = asyncio.run(handle_write_decision({
                "key": "new-decision-orphan-supersedes",
                "title": "Decision with orphan supersedes",
                "decision": "Do Y.",
                "rationale": "Because Y is better.",
                "date": "2026-04",
                "supersedes": "nonexistent-key",
            }))

            # write_decision must succeed (no error)
            assert "error" not in result[0].text.lower()
            assert "stored" in result[0].text.lower() or "decision" in result[0].text.lower()

            # list_decisions must return the new decision
            list_result = asyncio.run(handle_list_decisions({}))

        text = list_result[0].text
        assert "new-decision-orphan-supersedes" in text
        # No crash or error message
        assert "error" not in text.lower()


class TestListDecisionsRetrieval:
    """Tests for list_decisions retrieval and display behavior."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp(prefix="lobster_test_listdec_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def static_provider(self, temp_dir):
        from src.mcp.memory.static_memory import StaticMemory
        return StaticMemory(
            canonical_dir=temp_dir / "canonical",
            event_log=temp_dir / "events.jsonl",
        )

    def test_empty_memory_returns_clear_message(self, static_provider):
        """list_decisions returns a clear no-decisions message when memory is empty."""
        from src.mcp.inbox_server import handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            result = asyncio.run(handle_list_decisions({}))

        assert "no" in result[0].text.lower()
        assert "decision" in result[0].text.lower()

    def test_returns_only_decision_type_events(self, static_provider):
        """list_decisions must not return non-decision memory events."""
        from src.mcp.memory.provider import MemoryEvent
        from src.mcp.inbox_server import handle_list_decisions

        # Store a note event (should NOT appear in list_decisions)
        static_provider.store(MemoryEvent(
            id=None,
            timestamp=datetime.now(timezone.utc),
            type="note",
            source="internal",
            project=None,
            content="[DECISION] key=fake-note — this is a note, not a decision",
            metadata={"tags": []},
        ))

        # Store a real decision
        static_provider.store(MemoryEvent(
            id=None,
            timestamp=datetime.now(timezone.utc),
            type=DECISION_TYPE,
            source="internal",
            project=None,
            content="[DECISION] key=real-decision\nTitle: Real\nDecision: Do X\nRationale: Because\nDate: 2026-04",
            metadata={
                "tags": ["architecture", "decision"],
                "decision_key": "real-decision",
                "decision_title": "Real",
                "decision_date": "2026-04",
                "decision_affected_areas": [],
            },
        ))

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            result = asyncio.run(handle_list_decisions({}))

        text = result[0].text
        assert "real-decision" in text
        # The note should not appear as a decision entry
        assert "fake-note" not in text

    def test_active_only_hides_superseded_decisions(self, static_provider):
        """active_only=True (default) must hide superseded decisions as standalone entries.

        The superseded key may appear as metadata in the new decision's content
        (e.g. in a 'Supersedes: ...' line) — what must be absent is the superseded
        decision as a top-level section header.
        """
        from src.mcp.inbox_server import handle_write_decision, handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            # Store the old decision
            asyncio.run(handle_write_decision({
                "key": "old-relay-pattern",
                "title": "Old relay pattern",
                "decision": "Use relay pattern.",
                "rationale": "Simple.",
                "date": "2025-01",
            }))

            # Store a new decision that supersedes the old one
            asyncio.run(handle_write_decision({
                "key": "direct-delivery",
                "title": "Direct delivery",
                "decision": "Use direct send_reply.",
                "rationale": "Crash-safe.",
                "date": "2026-04",
                "supersedes": "old-relay-pattern",
            }))

            result = asyncio.run(handle_list_decisions({"active_only": True}))

        text = result[0].text
        # The new decision must appear as a section header
        assert "### direct-delivery" in text
        # The old decision must NOT appear as a section header (it is superseded)
        assert "### old-relay-pattern" not in text
        # The active count must reflect only 1 active decision
        assert "(1 active)" in text

    def test_active_only_false_shows_all_decisions(self, static_provider):
        """active_only=False must show all decisions including superseded ones."""
        from src.mcp.inbox_server import handle_write_decision, handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            asyncio.run(handle_write_decision({
                "key": "old-decision",
                "title": "Old decision",
                "decision": "Use X.",
                "rationale": "Historical.",
                "date": "2025-01",
            }))
            asyncio.run(handle_write_decision({
                "key": "new-decision",
                "title": "New decision",
                "decision": "Use Y.",
                "rationale": "Better.",
                "date": "2026-04",
                "supersedes": "old-decision",
            }))

            result = asyncio.run(handle_list_decisions({"active_only": False}))

        text = result[0].text
        assert "old-decision" in text
        assert "new-decision" in text
        # The superseded decision must be labelled — regression guard for display logic
        assert "[SUPERSEDED]" in text
        assert "[SUPERSEDED] old-decision" in text

    def test_area_filter_narrows_results(self, static_provider):
        """list_decisions with area filter returns only decisions affecting that area."""
        from src.mcp.inbox_server import handle_write_decision, handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            asyncio.run(handle_write_decision({
                "key": "subagent-comm-decision",
                "title": "Subagent comm decision",
                "decision": "Use direct delivery.",
                "rationale": "Crash safe.",
                "date": "2026-04",
                "affected_areas": ["subagent-communication"],
            }))
            asyncio.run(handle_write_decision({
                "key": "db-schema-decision",
                "title": "DB schema decision",
                "decision": "Use SQLite.",
                "rationale": "Simple.",
                "date": "2026-04",
                "affected_areas": ["database"],
            }))

            result = asyncio.run(handle_list_decisions({"area": "subagent-communication"}))

        text = result[0].text
        assert "subagent-comm-decision" in text
        # DB decision should not appear when filtering by subagent-communication
        assert "db-schema-decision" not in text

    def test_area_filter_no_matches_returns_clear_message(self, static_provider):
        """list_decisions with area filter and no matches returns a clear message."""
        from src.mcp.inbox_server import handle_write_decision, handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            asyncio.run(handle_write_decision({
                "key": "some-decision",
                "title": "Some decision",
                "decision": "Do X.",
                "rationale": "Because.",
                "date": "2026-04",
                "affected_areas": ["event-bus"],
            }))

            result = asyncio.run(handle_list_decisions({"area": "nonexistent-area-xyz"}))

        assert "no" in result[0].text.lower()
        assert "decision" in result[0].text.lower()

    def test_unavailable_memory_returns_error(self):
        """list_decisions returns an error when memory provider is None."""
        from src.mcp.inbox_server import handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", None):
            result = asyncio.run(handle_list_decisions({}))

        assert "not available" in result[0].text.lower()

    def test_displays_title_and_rationale(self, static_provider):
        """list_decisions output must include the title and rationale for each decision."""
        from src.mcp.inbox_server import handle_write_decision, handle_list_decisions

        with patch("src.mcp.inbox_server._memory_provider", static_provider):
            asyncio.run(handle_write_decision({
                "key": "nats-transport",
                "title": "NATS chosen for event bus",
                "decision": "Use NATS for all inter-service messaging.",
                "rationale": "Lower latency than alternatives.",
                "date": "2026-03",
                "affected_areas": ["event-bus"],
            }))

            result = asyncio.run(handle_list_decisions({}))

        text = result[0].text
        assert "NATS chosen for event bus" in text
        assert "Lower latency than alternatives" in text


class TestBuildDecisionContent:
    """Tests for the _build_decision_content pure function."""

    def test_canonical_content_format(self):
        """_build_decision_content produces a well-formed canonical string."""
        from src.mcp.inbox_server import _build_decision_content

        content = _build_decision_content(
            key="relay-pattern-deprecated",
            title="Relay pattern deprecated",
            decision="Use direct send_reply.",
            rationale="Crash-safe delivery.",
            date="2026-04",
            affected_areas=["subagent-communication", "write_result"],
            supersedes=None,
        )

        assert "[DECISION] key=relay-pattern-deprecated" in content
        assert "Title: Relay pattern deprecated" in content
        assert "Decision: Use direct send_reply." in content
        assert "Rationale: Crash-safe delivery." in content
        assert "Date: 2026-04" in content
        assert "subagent-communication" in content
        assert "write_result" in content
        # No supersedes line when not provided
        assert "Supersedes:" not in content

    def test_supersedes_included_when_provided(self):
        """_build_decision_content includes the Supersedes line when provided."""
        from src.mcp.inbox_server import _build_decision_content

        content = _build_decision_content(
            key="new-pattern",
            title="New pattern",
            decision="Use Y.",
            rationale="Better.",
            date="2026-04",
            affected_areas=[],
            supersedes="old-pattern",
        )

        assert "Supersedes: old-pattern" in content

    def test_empty_affected_areas_omitted(self):
        """_build_decision_content omits the Affected areas line when list is empty."""
        from src.mcp.inbox_server import _build_decision_content

        content = _build_decision_content(
            key="minimal-decision",
            title="Minimal",
            decision="Do X.",
            rationale="Because.",
            date="2026-04",
            affected_areas=[],
            supersedes=None,
        )

        assert "Affected areas:" not in content
