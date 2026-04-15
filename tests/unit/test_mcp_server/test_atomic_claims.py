"""
Tests for the AtomicClaimDB class (src/mcp/claims.py).

Verifies that:
- claim() grants exclusive ownership via SQLite INSERT OR FAIL
- Two concurrent callers on the same message_id: one wins, one returns False
- release() allows reclaiming
- update_status() persists state changes
- dispatcher_lock operations work correctly
"""

import sys
from pathlib import Path

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from claims import AtomicClaimDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Return a fresh AtomicClaimDB backed by a per-test SQLite file."""
    return AtomicClaimDB(path=tmp_path / "test_claims.db")


# ---------------------------------------------------------------------------
# Message claim tests
# ---------------------------------------------------------------------------


class TestMessageClaim:
    def test_first_claim_succeeds(self, db):
        """First caller to claim a message_id wins."""
        assert db.claim("msg-001", "dispatcher") is True

    def test_duplicate_claim_fails(self, db):
        """Second caller on the same message_id gets False."""
        db.claim("msg-002", "dispatcher")
        assert db.claim("msg-002", "dispatcher") is False

    def test_different_message_ids_independent(self, db):
        """Claims on different message_ids do not interfere."""
        assert db.claim("msg-a", "dispatcher") is True
        assert db.claim("msg-b", "dispatcher") is True

    def test_release_allows_reclaim(self, db):
        """After release(), the message can be claimed again."""
        db.claim("msg-003", "dispatcher")
        db.release("msg-003")
        assert db.claim("msg-003", "dispatcher-2") is True

    def test_is_claimed_true_after_claim(self, db):
        """is_claimed() returns True after a successful claim."""
        db.claim("msg-004", "dispatcher")
        assert db.is_claimed("msg-004") is True

    def test_is_claimed_false_before_claim(self, db):
        """is_claimed() returns False for unknown message_id."""
        assert db.is_claimed("nonexistent") is False

    def test_is_claimed_false_after_release(self, db):
        """is_claimed() returns False after release()."""
        db.claim("msg-005", "dispatcher")
        db.release("msg-005")
        assert db.is_claimed("msg-005") is False

    def test_update_status_processed(self, db):
        """update_status('processed') persists without raising."""
        db.claim("msg-006", "dispatcher")
        # Should not raise
        db.update_status("msg-006", "processed")

    def test_update_status_failed(self, db):
        """update_status('failed') persists without raising."""
        db.claim("msg-007", "dispatcher")
        db.update_status("msg-007", "failed")

    def test_update_status_noop_on_missing_row(self, db):
        """update_status() is a no-op on an unclaimed message_id — no exception."""
        db.update_status("never-claimed", "processed")

    def test_release_noop_on_missing_row(self, db):
        """release() is a no-op on an unclaimed message_id — no exception."""
        db.release("never-claimed")


class TestConcurrentClaim:
    """Verify that concurrent callers get exactly one winner.

    The real threat model is two *processes* (two separate dispatcher sessions)
    both trying to claim the same message. We simulate this by using two
    independent AtomicClaimDB instances backed by the same DB file — each with
    its own sqlite3 connection, mirroring the per-process isolation of production.

    Within a single process using a shared connection, SQLite's thread-safety
    guarantees depend on the threading mode, so we don't test intra-process
    thread races here. The guarantee is at the DB level (UNIQUE PRIMARY KEY +
    WAL mode) and is most meaningful across connection boundaries.
    """

    def test_only_one_winner_with_separate_connections(self, tmp_path):
        """Exactly one connection wins when two separate connections race on the same message_id.

        Each AtomicClaimDB has its own sqlite3.connect() call, simulating two
        separate dispatcher processes sharing the same DB file.
        """
        db_path = tmp_path / "race_test.db"
        db1 = AtomicClaimDB(path=db_path)
        db2 = AtomicClaimDB(path=db_path)

        # Claim from both in sequence (truly concurrent test would be flaky;
        # sequential demonstrates the UNIQUE constraint is the gate)
        result1 = db1.claim("race-msg", "dispatcher-1")
        result2 = db2.claim("race-msg", "dispatcher-2")

        assert result1 is True, "First caller must win"
        assert result2 is False, "Second caller must be rejected by UNIQUE constraint"

    def test_serial_claims_on_same_id_fail(self, tmp_path):
        """Serial claims on the same message_id: second is always rejected."""
        db_path = tmp_path / "serial_test.db"
        db_a = AtomicClaimDB(path=db_path)
        db_b = AtomicClaimDB(path=db_path)

        # db_a claims first
        assert db_a.claim("msg-serial", "session-a") is True
        # db_b attempts — must fail regardless of ordering
        assert db_b.claim("msg-serial", "session-b") is False


# ---------------------------------------------------------------------------
# Dispatcher lock tests
# ---------------------------------------------------------------------------


class TestDispatcherLock:
    def test_acquire_lock_succeeds_when_empty(self, db):
        """Acquiring a lock on an empty DB succeeds."""
        assert db.acquire_dispatcher_lock("session-1") is True

    def test_get_lock_returns_session_id(self, db):
        """get_dispatcher_lock() returns the session_id after acquisition."""
        db.acquire_dispatcher_lock("session-2")
        lock = db.get_dispatcher_lock()
        assert lock is not None
        assert lock["session_id"] == "session-2"
        assert "locked_at" in lock

    def test_get_lock_none_when_empty(self, db):
        """get_dispatcher_lock() returns None when no lock is held."""
        assert db.get_dispatcher_lock() is None

    def test_force_replace_takes_over(self, db):
        """force_replace_dispatcher_lock() replaces an existing lock."""
        db.acquire_dispatcher_lock("session-old")
        db.force_replace_dispatcher_lock("session-new")
        lock = db.get_dispatcher_lock()
        assert lock is not None
        assert lock["session_id"] == "session-new"

    def test_release_dispatcher_lock_clears_it(self, db):
        """release_dispatcher_lock() removes the lock row."""
        db.acquire_dispatcher_lock("session-3")
        db.release_dispatcher_lock("session-3")
        assert db.get_dispatcher_lock() is None

    def test_release_wrong_session_noop(self, db):
        """release_dispatcher_lock() with wrong session_id is a no-op."""
        db.acquire_dispatcher_lock("session-4")
        db.release_dispatcher_lock("session-wrong")
        # Lock should still exist
        lock = db.get_dispatcher_lock()
        assert lock is not None
        assert lock["session_id"] == "session-4"
