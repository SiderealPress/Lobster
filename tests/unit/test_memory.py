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
import threading
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

    def test_rotate_if_needed_fires_at_threshold(self, temp_dir):
        """File at or above 1 GB triggers rename to <name>.1."""
        from src.mcp.memory.static_memory import StaticMemory, _ROTATION_THRESHOLD
        event_log = temp_dir / "memory-events.jsonl"
        canonical_dir = temp_dir / "canonical"
        mem = StaticMemory(canonical_dir=canonical_dir, event_log=event_log)

        # Write a file that is exactly at the threshold
        event_log.write_bytes(b"x" * _ROTATION_THRESHOLD)
        mem._rotate_if_needed()

        rotated = event_log.with_suffix(event_log.suffix + ".1")
        assert rotated.exists(), "Rotated file .1 should exist after rotation"
        assert not event_log.exists(), "Original file should be gone after rotation"

    def test_rotate_if_needed_skips_below_threshold(self, temp_dir):
        """File below 1 GB leaves the log file untouched."""
        from src.mcp.memory.static_memory import StaticMemory, _ROTATION_THRESHOLD
        event_log = temp_dir / "memory-events.jsonl"
        canonical_dir = temp_dir / "canonical"
        mem = StaticMemory(canonical_dir=canonical_dir, event_log=event_log)

        event_log.write_bytes(b"x" * (_ROTATION_THRESHOLD - 1))
        mem._rotate_if_needed()

        assert event_log.exists(), "Original file should still exist below threshold"
        rotated = event_log.with_suffix(event_log.suffix + ".1")
        assert not rotated.exists(), "No .1 file should be created below threshold"

    def test_rotate_if_needed_clobbers_existing_rotation(self, temp_dir):
        """A second rotation overwrites any existing .1 file."""
        from src.mcp.memory.static_memory import StaticMemory, _ROTATION_THRESHOLD
        event_log = temp_dir / "memory-events.jsonl"
        canonical_dir = temp_dir / "canonical"
        rotated = event_log.with_suffix(event_log.suffix + ".1")

        mem = StaticMemory(canonical_dir=canonical_dir, event_log=event_log)

        # Simulate a stale .1 file from a previous rotation
        rotated.write_text("old rotation content")

        # Write a new log that exceeds the threshold
        event_log.write_bytes(b"y" * _ROTATION_THRESHOLD)
        mem._rotate_if_needed()

        assert rotated.exists(), "Rotated .1 file should exist"
        # The stale content should have been overwritten
        assert rotated.read_bytes() == b"y" * _ROTATION_THRESHOLD, (
            "Existing .1 file should be clobbered by the new rotation"
        )


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
        pytest.importorskip("sqlite_vec", reason="sqlite_vec not installed — skipping VectorMemory tests")
        pytest.importorskip("fastembed", reason="fastembed not installed — skipping VectorMemory tests")
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
# SQLite Resilience Tests (issue #2206 regression)
# ============================================================================
#
# memory.db is shared between VectorMemory and the User Model subsystem,
# each holding its own independent long-lived connection in WAL mode. A
# write on one connection can collide with a write held open on another,
# raising sqlite3.OperationalError("database is locked"). Prior to the
# fix, this happened with zero retry and silently dropped the event.


class TestSqliteResilienceHelpers:
    """Pure unit tests for the retry/backoff primitives (no DB needed)."""

    def test_is_retryable_lock_error_true_for_locked_db(self):
        import sqlite3
        from src.mcp.memory.sqlite_resilience import is_retryable_lock_error

        exc = sqlite3.OperationalError("database is locked")
        assert is_retryable_lock_error(exc) is True

    def test_is_retryable_lock_error_true_for_locked_table(self):
        import sqlite3
        from src.mcp.memory.sqlite_resilience import is_retryable_lock_error

        exc = sqlite3.OperationalError("database table is locked")
        assert is_retryable_lock_error(exc) is True

    def test_is_retryable_lock_error_false_for_other_operational_errors(self):
        import sqlite3
        from src.mcp.memory.sqlite_resilience import is_retryable_lock_error

        exc = sqlite3.OperationalError("no such table: events")
        assert is_retryable_lock_error(exc) is False

    def test_is_retryable_lock_error_false_for_non_sqlite_errors(self):
        from src.mcp.memory.sqlite_resilience import is_retryable_lock_error

        assert is_retryable_lock_error(ValueError("not sqlite")) is False

    def test_with_retry_succeeds_after_transient_failures(self):
        import sqlite3
        from src.mcp.memory.sqlite_resilience import with_retry

        calls = {"n": 0}
        sleeps = []

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        result = with_retry(flaky, max_attempts=5, sleep=sleeps.append)
        assert result == "ok"
        assert calls["n"] == 3
        # Two retries happened, so two backoff sleeps were recorded.
        assert len(sleeps) == 2

    def test_with_retry_raises_after_exhausting_attempts(self):
        import sqlite3
        from src.mcp.memory.sqlite_resilience import with_retry

        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError):
            with_retry(always_locked, max_attempts=3, sleep=lambda s: None)

    def test_with_retry_does_not_retry_non_retryable_errors(self):
        import sqlite3
        from src.mcp.memory.sqlite_resilience import with_retry

        calls = {"n": 0}

        def bad_sql():
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: events")

        with pytest.raises(sqlite3.OperationalError):
            with_retry(bad_sql, max_attempts=5, sleep=lambda s: None)
        # Should fail immediately on the first attempt - no retry loop entered.
        assert calls["n"] == 1

    def test_write_result_success_and_failure_shapes(self):
        from src.mcp.memory.sqlite_resilience import WriteResult

        ok = WriteResult.success(42, attempts=2)
        assert ok.ok is True
        assert ok.value == 42
        assert ok.error is None

        err = ValueError("boom")
        failed = WriteResult.failure(err, attempts=5)
        assert failed.ok is False
        assert failed.value is None
        assert failed.error is err


class TestVectorMemoryLockContention:
    """Reproduces sustained SQLITE_BUSY contention against a real on-disk
    database (mirroring the User Model subsystem holding an independent
    write lock on the same memory.db) and proves the fix handles it
    without silently dropping the event.
    """

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp(prefix="lobster_test_vecmem_lock_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def vec_mem(self, temp_dir):
        pytest.importorskip("sqlite_vec", reason="sqlite_vec not installed — skipping VectorMemory tests")
        pytest.importorskip("fastembed", reason="fastembed not installed — skipping VectorMemory tests")
        from src.mcp.memory.vector_memory import VectorMemory
        db_path = temp_dir / "test_memory_lock.db"
        return VectorMemory(db_path=db_path)

    def _make_event(self, content: str):
        from src.mcp.memory.provider import MemoryEvent
        return MemoryEvent(
            id=None, timestamp=datetime.now(timezone.utc),
            type="note", source="internal", project=None,
            content=content,
        )

    def test_busy_timeout_is_set_on_connection(self, vec_mem):
        """The connection must configure a busy_timeout so SQLite itself
        retries internally before ever raising "database is locked"."""
        row = vec_mem._conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] > 0

    def test_store_survives_concurrent_writer_holding_lock(self, vec_mem, temp_dir):
        """Simulates the User Model subsystem's independent connection
        holding the SQLite write lock (BEGIN IMMEDIATE) on the same
        physical file while VectorMemory.store() is called. Before the
        fix, this raised sqlite3.OperationalError immediately with no
        retry and the event was lost. After the fix, VectorMemory's own
        busy_timeout (set in _init_db) lets SQLite's internal busy
        handler wait out the lock, so the write succeeds.
        """
        db_path = temp_dir / "test_memory_lock.db"
        release_event = threading.Event()
        blocker_ready = threading.Event()

        def hold_lock_in_own_thread():
            # sqlite3 connections are thread-affine: this connection must be
            # created AND released from the same thread, which is why the
            # blocker runs in its own thread rather than being shared.
            blocker = sqlite3.connect(str(db_path), timeout=0)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute("SELECT 1")  # ensure the write lock is actually held
            blocker_ready.set()
            release_event.wait(timeout=5)
            blocker.commit()
            blocker.close()

        holder = threading.Thread(target=hold_lock_in_own_thread)
        holder.start()
        blocker_ready.wait(timeout=5)
        try:
            # Release the lock shortly after VectorMemory starts retrying,
            # well within its busy_timeout + retry/backoff window.
            threading.Timer(0.3, release_event.set).start()
            event = self._make_event("Event written during lock contention")
            event_id = vec_mem.store(event)
        finally:
            holder.join(timeout=5)

        assert event_id >= 1
        assert event.id == event_id
        # The event must actually be persisted, not merely return an ID.
        assert vec_mem.event_count() == 1
        recent = vec_mem.recent(hours=1)
        assert any(e.content == "Event written during lock contention" for e in recent)

    def test_try_store_exhausts_retries_and_returns_explicit_failure(self, vec_mem, temp_dir):
        """When contention is sustained beyond all retries (the blocker
        never releases the lock within VectorMemory's retry budget),
        try_store must return an explicit WriteResult(ok=False) - not
        raise unexpectedly, not silently return a sentinel ID, and not
        swallow the error. The underlying event must not be left
        half-written.
        """
        db_path = temp_dir / "test_memory_lock.db"
        blocker_ready = threading.Event()
        stop_blocking = threading.Event()

        def hold_lock_forever():
            blocker = sqlite3.connect(str(db_path), timeout=0)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute("SELECT 1")
            blocker_ready.set()
            # Must outlast the full retry budget below: busy_timeout (5s,
            # set in VectorMemory._init_db) x max_attempts(2) can take up
            # to ~10s before try_store gives up, plus backoff between
            # attempts. 30s gives ample headroom so this test deterministically
            # exercises exhaustion rather than racing a lucky late success.
            stop_blocking.wait(timeout=30)
            blocker.rollback()
            blocker.close()

        holder = threading.Thread(target=hold_lock_forever)
        holder.start()
        blocker_ready.wait(timeout=5)
        try:
            event = self._make_event("This write should fail loudly, not vanish")
            # max_attempts=2 keeps this deterministic and fast: the blocker
            # thread holds the lock for the whole call, so every attempt -
            # including SQLite's own internal busy_timeout wait - must fail,
            # proving retries truly exhaust rather than happening to win a race.
            result = vec_mem.try_store(event, max_attempts=2)

            assert result.ok is False
            assert isinstance(result.error, sqlite3.OperationalError)
            assert result.attempts == 2
            # No row should have been committed for this event.
            assert vec_mem.event_count() == 0

            # store() (the raising variant) must re-raise rather than swallow.
            with pytest.raises(sqlite3.OperationalError):
                vec_mem.store(self._make_event("Also should raise, not vanish"), max_attempts=2)
        finally:
            stop_blocking.set()
            holder.join(timeout=5)

    def test_health_check_reports_healthy_with_recent_write(self, vec_mem):
        event = self._make_event("Recent write for health check")
        vec_mem.store(event)

        health = vec_mem.health_check(stale_after_hours=24)
        assert health["healthy"] is True
        assert health["event_count"] == 1
        assert health["last_write"] is not None
        assert health["reason"] is None

    def test_health_check_reports_unhealthy_when_stale(self, vec_mem):
        # Insert directly with a stale timestamp to simulate a pipeline
        # that stopped writing N hours ago (issue #2206's actual symptom).
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        vec_mem._conn.execute(
            """
            INSERT INTO events (timestamp, type, source, project, content, metadata, consolidated)
            VALUES (?, 'note', 'internal', NULL, 'Stale event', '{}', 0)
            """,
            (stale_ts,),
        )
        vec_mem._conn.commit()

        health = vec_mem.health_check(stale_after_hours=24)
        assert health["healthy"] is False
        assert health["event_count"] == 1
        assert "stall" in health["reason"]

    def test_health_check_healthy_when_empty(self, vec_mem):
        health = vec_mem.health_check()
        assert health["healthy"] is True
        assert health["event_count"] == 0
        assert health["last_write"] is None


# ============================================================================
# CPU Logging Tests
# ============================================================================


class TestCPULogging:
    """Tests that CPU usage is logged during embedding operations."""

    def test_embedding_logs_cpu(self, caplog):
        """Verify that embedding operations log CPU usage."""
        pytest.importorskip("fastembed", reason="fastembed not installed — skipping EmbeddingModel tests")
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
        pytest.importorskip("fastembed", reason="fastembed not installed — skipping EmbeddingModel tests")
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

    def test_falls_back_to_static(self, tmp_path):
        """Test fallback to StaticMemory when VectorMemory fails."""
        import src.mcp.memory.static_memory as _sm
        from src.mcp.memory import create_memory_provider
        from src.mcp.memory.static_memory import StaticMemory

        canonical_dir = tmp_path / "canonical"
        canonical_dir.mkdir()
        event_log = tmp_path / "events.jsonl"
        with (
            patch("src.mcp.memory.VectorMemory", side_effect=ImportError("no sqlite-vec")),
            patch.object(_sm, "DEFAULT_CANONICAL_DIR", canonical_dir),
        ):
            provider = create_memory_provider(use_vector=True)
            assert isinstance(provider, StaticMemory)

    def test_force_static(self, tmp_path):
        """Test forcing StaticMemory with use_vector=False."""
        import src.mcp.memory.static_memory as _sm
        from src.mcp.memory import create_memory_provider
        from src.mcp.memory.static_memory import StaticMemory

        canonical_dir = tmp_path / "canonical"
        canonical_dir.mkdir()
        with patch.object(_sm, "DEFAULT_CANONICAL_DIR", canonical_dir):
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
        pytest.importorskip("sqlite_vec", reason="sqlite_vec not installed — skipping VectorMemory tests")
        pytest.importorskip("fastembed", reason="fastembed not installed — skipping VectorMemory tests")
        from src.mcp.memory.vector_memory import VectorMemory
        from src.mcp.memory.provider import MemoryEvent

        db_path = temp_dir / "e2e_test.db"
        provider = VectorMemory(db_path=db_path)

        # Store diverse events
        events_data = [
            ("The deployment pipeline was updated to use Docker", "decision", "github", "infra"),
            ("Alice asked about progress on the memory system", "message", "telegram", "lobster"),
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
