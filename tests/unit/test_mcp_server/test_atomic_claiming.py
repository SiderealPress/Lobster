"""
Unit tests for atomic message claiming (issue #1360).

Covers:
- claims.py: claim_message returns True on first call, False on duplicate
- claims.py: release_claim allows re-claiming after release
- claims.py: dispatcher lock acquire/release semantics
- handle_claim_and_ack: returns already_claimed on second concurrent call
- handle_mark_processing: returns already_claimed on second concurrent call
- _recover_stale_processing: releases claim before returning message to inbox
"""

import json
import sys
import asyncio
import pytest
from pathlib import Path
from unittest.mock import patch

# Ensure src/mcp is on sys.path
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401 — pre-load for patch.multiple


# =============================================================================
# claims.py unit tests (pure SQLite, no inbox_server dependency)
# =============================================================================


class TestClaimMessage:
    """Tests for the claim_message / release_claim functions in claims.py."""

    @pytest.fixture(autouse=True)
    def isolated_claims_db(self, tmp_path):
        """Redirect claims DB to a temp file for each test."""
        import claims
        claims._set_db_path(tmp_path / "test-claims.db")
        yield
        # Reset to default so other tests are unaffected
        claims._set_db_path(None)

    def test_first_claim_returns_true(self):
        """claim_message returns True when no existing claim is held."""
        from claims import claim_message
        result = claim_message("msg_001")
        assert result is True

    def test_duplicate_claim_returns_false(self):
        """claim_message returns False when the same message_id is already claimed."""
        from claims import claim_message
        first = claim_message("msg_002")
        second = claim_message("msg_002")
        assert first is True
        assert second is False

    def test_different_message_ids_are_independent(self):
        """Two different message IDs can both be claimed simultaneously."""
        from claims import claim_message
        assert claim_message("msg_003") is True
        assert claim_message("msg_004") is True

    def test_release_allows_reclaim(self):
        """After release_claim, the same message_id can be claimed again."""
        from claims import claim_message, release_claim
        assert claim_message("msg_005") is True
        assert claim_message("msg_005") is False  # duplicate rejected
        release_claim("msg_005")
        assert claim_message("msg_005") is True  # re-claim succeeds after release

    def test_release_on_unclaimed_message_is_noop(self):
        """release_claim on a message with no row does not raise."""
        from claims import release_claim
        # Should not raise even if no row exists
        release_claim("nonexistent_msg")

    def test_claim_with_session_id(self):
        """session_id is stored alongside the claim and does not affect uniqueness."""
        from claims import claim_message, release_claim
        result = claim_message("msg_006", session_id="session-abc")
        assert result is True
        # Same message_id with different session_id still fails — ownership is by message_id
        result2 = claim_message("msg_006", session_id="session-xyz")
        assert result2 is False


class TestDispatcherLock:
    """Tests for acquire_dispatcher_lock / release_dispatcher_lock."""

    @pytest.fixture(autouse=True)
    def isolated_claims_db(self, tmp_path):
        import claims
        claims._set_db_path(tmp_path / "test-dispatcher.db")
        yield
        claims._set_db_path(None)

    def test_first_acquire_returns_true(self):
        """acquire_dispatcher_lock returns True when no lock is held."""
        from claims import acquire_dispatcher_lock
        assert acquire_dispatcher_lock("session-A") is True

    def test_same_session_can_renew(self):
        """The same session_id can call acquire_dispatcher_lock again (renewal)."""
        from claims import acquire_dispatcher_lock
        assert acquire_dispatcher_lock("session-A") is True
        assert acquire_dispatcher_lock("session-A") is True

    def test_different_session_blocked(self):
        """A second session_id is blocked while the first holds the lock."""
        from claims import acquire_dispatcher_lock
        assert acquire_dispatcher_lock("session-A") is True
        assert acquire_dispatcher_lock("session-B") is False

    def test_release_allows_new_session(self):
        """After release, a different session_id can acquire the lock."""
        from claims import acquire_dispatcher_lock, release_dispatcher_lock
        assert acquire_dispatcher_lock("session-A") is True
        release_dispatcher_lock("session-A")
        assert acquire_dispatcher_lock("session-B") is True

    def test_release_by_non_holder_is_noop(self):
        """Releasing with a session_id that does not hold the lock does nothing."""
        from claims import acquire_dispatcher_lock, release_dispatcher_lock, is_dispatcher_locked_by
        acquire_dispatcher_lock("session-A")
        release_dispatcher_lock("session-B")  # should not release A's lock
        assert is_dispatcher_locked_by("session-A") is True

    def test_is_dispatcher_locked_by(self):
        """is_dispatcher_locked_by returns True only for the current holder."""
        from claims import acquire_dispatcher_lock, is_dispatcher_locked_by
        acquire_dispatcher_lock("session-X")
        assert is_dispatcher_locked_by("session-X") is True
        assert is_dispatcher_locked_by("session-Y") is False


# =============================================================================
# handle_claim_and_ack integration: already_claimed on second call
# =============================================================================


class TestClaimAndAckAtomicGuard:
    """Tests that handle_claim_and_ack returns already_claimed on duplicate."""

    @pytest.fixture
    def dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        outbox = temp_messages_dir / "outbox"
        sent = temp_messages_dir / "sent"
        sent.mkdir(exist_ok=True)
        return inbox, processing, outbox, sent

    @pytest.fixture(autouse=True)
    def isolated_claims_db(self, tmp_path):
        """Redirect claims DB to a temp file and ensure _http_session_manager is None."""
        import claims
        claims._set_db_path(tmp_path / "test-claims-ack.db")
        yield
        claims._set_db_path(None)

    def _write_inbox_message(self, inbox: Path, msg_id: str = "1700000000000_telegram") -> str:
        msg = {
            "id": msg_id,
            "source": "telegram",
            "chat_id": 123456,
            "type": "text",
            "text": "Do the thing",
            "timestamp": "2026-03-16T10:00:00.000000",
        }
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))
        return msg_id

    def test_first_claim_and_ack_succeeds(self, dirs):
        """First claim_and_ack on an inbox message succeeds normally."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            _http_session_manager=None,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack
            result = asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        assert "already_claimed" not in result[0].text
        assert (processing / f"{msg_id}.json").exists()

    def test_second_claim_and_ack_returns_already_claimed(self, dirs):
        """Second call on the same message_id returns already_claimed (SQLite guard)."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            _http_session_manager=None,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            # First call wins the claim
            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

            # Second call must return already_claimed — no ack sent, no agent spawned
            result = asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "On it.",
                "chat_id": 123456,
                "source": "telegram",
            }))

        assert "already_claimed" in result[0].text, (
            f"Expected 'already_claimed' in result, got: {result[0].text!r}"
        )
        # Only one ack file written (from the winning call)
        outbox_files = list(outbox.glob("*.json"))
        assert len(outbox_files) == 1, (
            f"Expected exactly 1 ack (from the winner), got {len(outbox_files)}"
        )

    def test_second_call_sends_no_ack(self, dirs):
        """The already_claimed response must not trigger an ack to the user."""
        inbox, processing, outbox, sent = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            OUTBOX_DIR=outbox,
            SENT_DIR=sent,
            _http_session_manager=None,
        ):
            from src.mcp.inbox_server import handle_claim_and_ack

            # Simulate: first call done, message is in processing/
            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "First ack.",
                "chat_id": 123456,
                "source": "telegram",
            }))
            outbox_before = len(list(outbox.glob("*.json")))

            # Second call
            asyncio.run(handle_claim_and_ack({
                "message_id": msg_id,
                "ack_text": "Second ack — must not be delivered.",
                "chat_id": 123456,
                "source": "telegram",
            }))
            outbox_after = len(list(outbox.glob("*.json")))

        assert outbox_after == outbox_before, (
            "No additional ack should be written when the claim is rejected"
        )


# =============================================================================
# handle_mark_processing integration: already_claimed on second call
# =============================================================================


class TestMarkProcessingAtomicGuard:
    """Tests that handle_mark_processing returns already_claimed on duplicate."""

    @pytest.fixture
    def dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        return inbox, processing

    @pytest.fixture(autouse=True)
    def isolated_claims_db(self, tmp_path):
        import claims
        claims._set_db_path(tmp_path / "test-claims-proc.db")
        yield
        claims._set_db_path(None)

    def _write_inbox_message(self, inbox: Path, msg_id: str = "1700000000001_telegram") -> str:
        msg = {
            "id": msg_id,
            "source": "telegram",
            "chat_id": 123456,
            "type": "text",
            "text": "Process me",
            "timestamp": "2026-03-16T10:00:01.000000",
        }
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))
        return msg_id

    def test_second_mark_processing_returns_already_claimed(self, dirs):
        """Second mark_processing on the same message_id returns already_claimed."""
        inbox, processing = dirs
        msg_id = self._write_inbox_message(inbox)

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
            _http_session_manager=None,
        ):
            from src.mcp.inbox_server import handle_mark_processing

            # First call claims the message
            result1 = asyncio.run(handle_mark_processing({"message_id": msg_id}))
            assert "already_claimed" not in result1[0].text

            # Second call must fail
            result2 = asyncio.run(handle_mark_processing({"message_id": msg_id}))
            assert "already_claimed" in result2[0].text, (
                f"Expected 'already_claimed' in result, got: {result2[0].text!r}"
            )


# =============================================================================
# _recover_stale_processing: releases claim before moving back to inbox
# =============================================================================


class TestRecoverStaleProcessingReleasesClaim:
    """_recover_stale_processing must release the claim row before rename."""

    @pytest.fixture(autouse=True)
    def isolated_claims_db(self, tmp_path):
        import claims
        claims._set_db_path(tmp_path / "test-claims-recover.db")
        yield
        claims._set_db_path(None)

    def test_stale_recovery_releases_claim_and_allows_reclaim(self, tmp_path):
        """After stale recovery, the message can be re-claimed by a new caller."""
        import time
        from claims import claim_message, release_claim
        from src.mcp import inbox_server as srv

        inbox = tmp_path / "inbox"
        processing = tmp_path / "processing"
        inbox.mkdir()
        processing.mkdir()

        msg_id = "1700000000002_telegram"
        msg = {
            "id": msg_id,
            "source": "telegram",
            "chat_id": 123456,
            "type": "text",
            "text": "I am stale",
            "timestamp": "2026-03-16T10:00:02.000000",
        }

        # Write directly to processing/ (simulating a stale in-progress message)
        proc_file = processing / f"{msg_id}.json"
        proc_file.write_text(json.dumps(msg))

        # Manually plant the claim row (simulating what handle_mark_processing did)
        claim_message(msg_id, session_id="old-session")
        # Confirm claim is held
        from claims import claim_message as _cm
        assert _cm(msg_id) is False  # already claimed

        # Age the file artificially by setting mtime to 200s ago (> 90s stale timeout)
        old_mtime = time.time() - 200
        import os
        os.utime(str(proc_file), (old_mtime, old_mtime))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            srv._recover_stale_processing()

        # File should now be in inbox/, not processing/
        assert not proc_file.exists(), "Stale file should have been moved out of processing/"
        assert (inbox / f"{msg_id}.json").exists(), "Stale file should be back in inbox/"

        # Claim should have been released — a new claim attempt succeeds
        from claims import claim_message as _cm2
        assert _cm2(msg_id) is True, (
            "After stale recovery, the claim row must be released so a fresh claim can win"
        )
