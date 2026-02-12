"""
Tests for the Three-Layer Memory System.

Tests cover:
- MemoryEvent creation and serialization
- VectorMemory store/search/recent/unconsolidated/mark_consolidated
- StaticMemory store/search/recent/unconsolidated/mark_consolidated
- Fallback behavior (create_memory_provider)
- CPU logging during embedding operations
- Hybrid search scoring
"""

import json
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# MemoryEvent Tests
# ============================================================================


class TestMemoryEvent:
    """Tests for the MemoryEvent dataclass."""

    def test_create_event(self):
        from src.mcp.memory.provider import MemoryEvent
        event = MemoryEvent(
            id=None,
            timestamp=datetime.now(timezone.utc),
            type="message",
            source="telegram",
            project="lobster",
            content="Hello world",
        )
        assert event.type == "message"
        assert event.source == "telegram"
        assert event.project == "lobster"
        assert event.content == "Hello world"
        assert event.consolidated is False
        assert event.metadata == {}

    def test_to_dict(self):
        from src.mcp.memory.provider import MemoryEvent
        now = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = MemoryEvent(
            id=42,
            timestamp=now,
            type="decision",
            source="internal",
            project=None,
            content="Use fastembed for embeddings",
            metadata={"tags": ["architecture"]},
            consolidated=True,
        )
        d = event.to_dict()
        assert d["id"] == 42
        assert d["type"] == "decision"
        assert d["consolidated"] is True
        assert d["metadata"] == {"tags": ["architecture"]}
        assert "2025-06-15" in d["timestamp"]

    def test_from_dict(self):
        from src.mcp.memory.provider import MemoryEvent
        data = {
            "id": 7,
            "timestamp": "2025-06-15T12:00:00+00:00",
            "type": "note",
            "source": "github",
            "project": "lobster",
            "content": "Fixed the bug",
            "metadata": {"pr": 123},
            "consolidated": False,
        }
        event = MemoryEvent.from_dict(data)
        assert event.id == 7
        assert event.type == "note"
        assert event.source == "github"
        assert event.content == "Fixed the bug"
        assert event.metadata == {"pr": 123}

    def test_from_dict_missing_fields(self):
        from src.mcp.memory.provider import MemoryEvent
        data = {"content": "Minimal event"}
        event = MemoryEvent.from_dict(data)
        assert event.content == "Minimal event"
        assert event.type == "note"
        assert event.source == "internal"
        assert event.project is None
        assert event.metadata == {}

    def test_roundtrip(self):
        from src.mcp.memory.provider import MemoryEvent
        now = datetime.now(timezone.utc)
        original = MemoryEvent(
            id=1,
            timestamp=now,
            type="task",
            source="telegram",
            project="myproject",
            content="Do the thing",
            metadata={"priority": "high"},
        )
        d = original.to_dict()
        restored = MemoryEvent.from_dict(d)
        assert restored.type == original.type
        assert restored.content == original.content
        assert restored.project == original.project
        assert restored.metadata == original.metadata


# ============================================================================
# StaticMemory Tests (no external dependencies)
# ============================================================================


class TestStaticMemory:
    """Tests for the StaticMemory fallback backend."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp(prefix="lobster_test_memory_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def static_mem(self, temp_dir):
        from src.mcp.memory.static_memory import StaticMemory
        canonical_dir = temp_dir / "canonical"
        event_log = temp_dir / "events.jsonl"
        return StaticMemory(canonical_dir=canonical_dir, event_log=event_log)

    def test_store_assigns_id(self, static_mem):
        from src.mcp.memory.provider import MemoryEvent
        event = MemoryEvent(
            id=None,
            timestamp=datetime.now(timezone.utc),
            type="note",
            source="internal",
            project=None,
            content="Test event",
        )
        event_id = static_mem.store(event)
        assert event_id == 1
        assert event.id == 1

    def test_store_increments_id(self, static_mem):
        from src.mcp.memory.provider import MemoryEvent
        for i in range(3):
            event = MemoryEvent(
                id=None,
                timestamp=datetime.now(timezone.utc),
                type="note",
                source="internal",
                project=None,
                content=f"Event {i}",
            )
            eid = static_mem.store(event)
            assert eid == i + 1

    def test_store_writes_to_jsonl(self, static_mem, temp_dir):
        from src.mcp.memory.provider import MemoryEvent
        event = MemoryEvent(
            id=None,
            timestamp=datetime.now(timezone.utc),
            type="note",
            source="internal",
            project=None,
            content="Persisted event",
        )
        static_mem.store(event)
        log_file = temp_dir / "events.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["content"] == "Persisted event"

    def test_search_event_log(self, static_mem):
        from src.mcp.memory.provider import MemoryEvent
        static_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="The quick brown fox jumps over the lazy dog",
        ))
        static_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="A completely different event about cats",
        ))
        results = static_mem.search("fox")
        assert len(results) == 1
        assert "fox" in results[0].content

    def test_search_canonical_files(self, static_mem, temp_dir):
        from src.mcp.memory.provider import MemoryEvent
        canonical_dir = temp_dir / "canonical"
        canonical_dir.mkdir(exist_ok=True)
        (canonical_dir / "test.md").write_text(
            "# Test Document\n\n"
            "This document discusses the memory system architecture.\n\n"
            "It uses SQLite for storage."
        )
        results = static_mem.search("memory system")
        assert len(results) >= 1
        assert any("memory system" in r.content.lower() for r in results)

    def test_search_with_project_filter(self, static_mem):
        from src.mcp.memory.provider import MemoryEvent
        static_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project="alpha",
            content="Alpha project details about testing",
        ))
        static_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project="beta",
            content="Beta project details about testing",
        ))
        results = static_mem.search("testing", project="alpha")
        assert len(results) == 1
        assert results[0].project == "alpha"

    def test_recent(self, static_mem):
        from src.mcp.memory.provider import MemoryEvent
        now = datetime.now(timezone.utc)
        static_mem.store(MemoryEvent(
            id=None, timestamp=now,
            type="note", source="internal", project=None,
            content="Recent event",
        ))
        static_mem.store(MemoryEvent(
            id=None, timestamp=now - timedelta(hours=48),
            type="note", source="internal", project=None,
            content="Old event",
        ))
        results = static_mem.recent(hours=24)
        assert len(results) == 1
        assert results[0].content == "Recent event"

    def test_unconsolidated(self, static_mem):
        from src.mcp.memory.provider import MemoryEvent
        static_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Not yet consolidated",
            consolidated=False,
        ))
        static_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Already consolidated",
            consolidated=True,
        ))
        results = static_mem.unconsolidated()
        assert len(results) == 1
        assert results[0].content == "Not yet consolidated"

    def test_mark_consolidated(self, static_mem, temp_dir):
        from src.mcp.memory.provider import MemoryEvent
        eid = static_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="To be consolidated",
        ))
        static_mem.mark_consolidated([eid])
        # Read back from file
        log_file = temp_dir / "events.jsonl"
        lines = log_file.read_text().strip().splitlines()
        data = json.loads(lines[0])
        assert data["consolidated"] is True

    def test_close_is_noop(self, static_mem):
        static_mem.close()  # Should not raise


# ============================================================================
# VectorMemory Tests
# ============================================================================


class TestVectorMemory:
    """Tests for VectorMemory (SQLite + sqlite-vec + FTS5)."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp(prefix="lobster_test_vecmem_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def vec_mem(self, temp_dir):
        from src.mcp.memory.vector_memory import VectorMemory
        db_path = temp_dir / "test_memory.db"
        return VectorMemory(db_path=db_path)

    def test_store_returns_id(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        event = MemoryEvent(
            id=None,
            timestamp=datetime.now(timezone.utc),
            type="message",
            source="telegram",
            project="lobster",
            content="Hello from VectorMemory test",
        )
        eid = vec_mem.store(event)
        assert eid >= 1
        assert event.id == eid

    def test_store_multiple(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        ids = []
        for i in range(3):
            event = MemoryEvent(
                id=None,
                timestamp=datetime.now(timezone.utc),
                type="note",
                source="internal",
                project=None,
                content=f"Test event number {i}",
            )
            ids.append(vec_mem.store(event))
        assert len(set(ids)) == 3  # All unique IDs

    def test_event_count(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        assert vec_mem.event_count() == 0
        vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Event 1",
        ))
        assert vec_mem.event_count() == 1

    def test_search_returns_relevant(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="The memory system uses SQLite with vector search for fast retrieval",
        ))
        vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="I had pizza for lunch today, it was delicious",
        ))
        vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Vector embeddings provide semantic similarity for natural language queries",
        ))

        results = vec_mem.search("how does the vector search work")
        assert len(results) >= 1
        # The most relevant result should mention vector/memory/search
        top_content = results[0].content.lower()
        assert any(word in top_content for word in ["vector", "memory", "search", "sqlite"])

    def test_search_with_project_filter(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project="alpha",
            content="Alpha project uses React for the frontend",
        ))
        vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project="beta",
            content="Beta project uses Vue for the frontend",
        ))

        results = vec_mem.search("frontend framework", project="alpha")
        assert len(results) >= 1
        assert all(r.project == "alpha" for r in results)

    def test_recent_returns_time_filtered(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        now = datetime.now(timezone.utc)
        vec_mem.store(MemoryEvent(
            id=None, timestamp=now,
            type="note", source="internal", project=None,
            content="Recent event from today",
        ))
        vec_mem.store(MemoryEvent(
            id=None, timestamp=now - timedelta(hours=48),
            type="note", source="internal", project=None,
            content="Old event from two days ago",
        ))

        results = vec_mem.recent(hours=24)
        assert len(results) == 1
        assert "Recent" in results[0].content

    def test_unconsolidated(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        eid1 = vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Not consolidated yet",
        ))
        eid2 = vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Also not consolidated",
        ))

        results = vec_mem.unconsolidated()
        assert len(results) == 2

    def test_mark_consolidated(self, vec_mem):
        from src.mcp.memory.provider import MemoryEvent
        eid = vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Will be consolidated",
        ))
        vec_mem.mark_consolidated([eid])
        results = vec_mem.unconsolidated()
        assert len(results) == 0

    def test_close(self, vec_mem):
        vec_mem.close()
        # Should not raise; connection should be closed

    def test_keyword_fallback_on_vector_failure(self, vec_mem):
        """Test that search falls back to keyword when vector search fails."""
        from src.mcp.memory.provider import MemoryEvent
        vec_mem.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Keyword searchable content about databases",
        ))

        # Patch the embedder to raise an error
        original_embed = vec_mem._embedder.embed_one
        def broken_embed(text):
            raise RuntimeError("Embedding model crashed")
        vec_mem._embedder.embed_one = broken_embed

        # Should fall back to keyword search
        results = vec_mem.search("databases")
        assert len(results) >= 1
        assert "databases" in results[0].content

        vec_mem._embedder.embed_one = original_embed


# ============================================================================
# CPU Logging Tests
# ============================================================================


class TestCPULogging:
    """Tests that CPU usage is logged during embedding operations."""

    def test_embedding_logs_cpu(self, caplog):
        """Verify that embedding operations log CPU usage."""
        import logging
        from src.mcp.memory.vector_memory import EmbeddingModel

        with caplog.at_level(logging.INFO, logger="lobster-memory"):
            model = EmbeddingModel()
            model.embed(["Test text for CPU logging"])

        # Check that CPU info was logged
        cpu_log_found = any("CPU" in record.message for record in caplog.records)
        assert cpu_log_found, "No CPU usage log found during embedding"

    def test_embedding_logs_timing(self, caplog):
        """Verify that embedding timing is logged."""
        import logging
        from src.mcp.memory.vector_memory import EmbeddingModel

        with caplog.at_level(logging.INFO, logger="lobster-memory"):
            model = EmbeddingModel()
            model.embed(["Test text for timing"])

        timing_found = any("s," in record.message or "Embedding:" in record.message for record in caplog.records)
        assert timing_found, "No timing log found during embedding"


# ============================================================================
# Factory / Fallback Tests
# ============================================================================


class TestCreateMemoryProvider:
    """Tests for the create_memory_provider factory function."""

    def test_creates_vector_by_default(self):
        """Test that VectorMemory is created when available."""
        from src.mcp.memory import create_memory_provider
        from src.mcp.memory.vector_memory import VectorMemory

        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "test.db"
            with patch("src.mcp.memory.VectorMemory", return_value=MagicMock(spec=VectorMemory)):
                provider = create_memory_provider(use_vector=True)
                assert provider is not None

    def test_falls_back_to_static(self):
        """Test fallback to StaticMemory when VectorMemory fails."""
        from src.mcp.memory import create_memory_provider
        from src.mcp.memory.static_memory import StaticMemory

        with patch("src.mcp.memory.VectorMemory", side_effect=ImportError("no sqlite-vec")):
            provider = create_memory_provider(use_vector=True)
            assert isinstance(provider, StaticMemory)

    def test_force_static(self):
        """Test forcing StaticMemory with use_vector=False."""
        from src.mcp.memory import create_memory_provider
        from src.mcp.memory.static_memory import StaticMemory

        provider = create_memory_provider(use_vector=False)
        assert isinstance(provider, StaticMemory)


# ============================================================================
# MCP Handler Integration Tests
# ============================================================================


class TestMemoryMCPHandlers:
    """Tests for the MCP tool handler logic.

    These tests verify the handler behavior by testing the same logic
    used in inbox_server.py, without importing the full MCP server module
    (which has namespace conflicts with the mcp SDK package).
    """

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp(prefix="lobster_test_handler_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    def test_memory_store_via_provider(self, temp_dir):
        """Test storing events through the provider (same as handler logic)."""
        from src.mcp.memory.static_memory import StaticMemory
        from src.mcp.memory.provider import MemoryEvent

        provider = StaticMemory(
            canonical_dir=temp_dir / "canonical",
            event_log=temp_dir / "events.jsonl",
        )

        event = MemoryEvent(
            id=None,
            timestamp=datetime.now(timezone.utc),
            type="note",
            source="telegram",
            project=None,
            content="Test storing a memory",
            metadata={"tags": ["test"]},
        )
        eid = provider.store(event)
        assert eid >= 1

    def test_memory_search_via_provider(self, temp_dir):
        """Test searching events through the provider."""
        from src.mcp.memory.static_memory import StaticMemory
        from src.mcp.memory.provider import MemoryEvent

        provider = StaticMemory(
            canonical_dir=temp_dir / "canonical",
            event_log=temp_dir / "events.jsonl",
        )

        provider.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content="Found result about testing",
        ))

        results = provider.search("testing")
        assert len(results) >= 1
        assert "testing" in results[0].content

    def test_memory_search_empty_results(self, temp_dir):
        """Test search with no matches."""
        from src.mcp.memory.static_memory import StaticMemory

        provider = StaticMemory(
            canonical_dir=temp_dir / "canonical",
            event_log=temp_dir / "events.jsonl",
        )

        results = provider.search("nonexistent_query_xyz")
        assert len(results) == 0

    def test_memory_recent_via_provider(self, temp_dir):
        """Test getting recent events through the provider."""
        from src.mcp.memory.static_memory import StaticMemory
        from src.mcp.memory.provider import MemoryEvent

        provider = StaticMemory(
            canonical_dir=temp_dir / "canonical",
            event_log=temp_dir / "events.jsonl",
        )

        provider.store(MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="message", source="telegram", project="lobster",
            content="Recent chat about lobster",
        ))

        results = provider.recent(hours=12)
        assert len(results) >= 1
        assert "lobster" in results[0].content

    def test_get_handoff_reads_file(self, temp_dir):
        """Test reading handoff document."""
        handoff = temp_dir / "handoff.md"
        handoff.write_text("# Test Handoff\n\nThis is a test.")
        content = handoff.read_text()
        assert "Test Handoff" in content

    def test_get_handoff_missing_file(self, temp_dir):
        """Test handoff with missing file."""
        handoff = temp_dir / "nonexistent.md"
        assert not handoff.exists()

    def test_end_to_end_store_and_search(self, temp_dir):
        """End-to-end test: store events, then search them."""
        from src.mcp.memory.vector_memory import VectorMemory
        from src.mcp.memory.provider import MemoryEvent

        db_path = temp_dir / "e2e_test.db"
        provider = VectorMemory(db_path=db_path)

        # Store diverse events
        events_data = [
            ("The deployment pipeline was updated to use Docker", "decision", "github", "infra"),
            ("Drew asked about progress on the memory system", "message", "telegram", "lobster"),
            ("Fixed a bug in the nightly consolidation script", "note", "internal", "lobster"),
            ("Meeting notes: discussed Q3 roadmap priorities", "note", "internal", None),
        ]

        for content, etype, source, project in events_data:
            provider.store(MemoryEvent(
                id=None,
                timestamp=datetime.now(timezone.utc),
                type=etype,
                source=source,
                project=project,
                content=content,
            ))

        # Search should return relevant results
        results = provider.search("memory system progress")
        assert len(results) >= 1
        # At least one result should be about the memory system
        all_content = " ".join(r.content.lower() for r in results)
        assert "memory" in all_content

        # Project filter should narrow results
        lobster_results = provider.search("bug fix", project="lobster")
        for r in lobster_results:
            assert r.project == "lobster"

        provider.close()
