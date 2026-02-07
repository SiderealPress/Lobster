"""
Tests for Daemon Session Management

Tests session ID creation, persistence, and resume functionality.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import uuid


class TestSessionManagement:
    """Tests for session ID management."""

    @pytest.fixture
    def workspace(self, temp_workspace: Path) -> Path:
        """Get workspace directory."""
        return temp_workspace

    def test_creates_new_session_id(self, workspace: Path):
        """Test that new session ID is created when none exists."""
        session_file = workspace / ".lobster_session_id"

        with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
            with patch("src.daemon.daemon.WORKSPACE", workspace):
                from src.daemon.daemon import get_or_create_session_id

                session_id = get_or_create_session_id()

                # Should be a valid UUID4
                assert session_id
                uuid.UUID(session_id)  # Will raise if invalid

                # Should be saved to file
                assert session_file.exists()
                assert session_file.read_text().strip() == session_id

    def test_returns_existing_session_id(self, workspace: Path):
        """Test that existing session ID is returned."""
        session_file = workspace / ".lobster_session_id"
        existing_id = str(uuid.uuid4())
        session_file.write_text(existing_id)

        with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
            from src.daemon.daemon import get_or_create_session_id

            session_id = get_or_create_session_id()

            assert session_id == existing_id

    def test_regenerates_if_file_empty(self, workspace: Path):
        """Test that new ID is generated if file is empty."""
        session_file = workspace / ".lobster_session_id"
        session_file.write_text("")

        with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
            from src.daemon.daemon import get_or_create_session_id

            session_id = get_or_create_session_id()

            # Should have generated a new ID
            assert session_id
            uuid.UUID(session_id)

    def test_session_has_been_used_false_initially(self, workspace: Path):
        """Test that session_has_been_used returns False initially."""
        marker = workspace / ".lobster_session_used"

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            from src.daemon.daemon import session_has_been_used

            assert session_has_been_used() is False

    def test_session_has_been_used_true_after_mark(self, workspace: Path):
        """Test that session_has_been_used returns True after marking."""
        marker = workspace / ".lobster_session_used"

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            from src.daemon.daemon import session_has_been_used, mark_session_used

            mark_session_used()

            assert session_has_been_used() is True
            assert marker.exists()

    def test_mark_session_used_writes_timestamp(self, workspace: Path):
        """Test that mark_session_used writes a timestamp."""
        marker = workspace / ".lobster_session_used"

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            from src.daemon.daemon import mark_session_used

            mark_session_used()

            content = marker.read_text()
            # Should be a numeric timestamp
            float(content)


class TestInboxHelpers:
    """Tests for inbox helper functions."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def test_count_inbox_messages_empty(self, inbox_dir: Path):
        """Test count with empty inbox."""
        with patch("src.daemon.daemon.INBOX_DIR", inbox_dir):
            from src.daemon.daemon import count_inbox_messages

            count = count_inbox_messages()
            assert count == 0

    def test_count_inbox_messages_with_files(
        self, inbox_dir: Path, message_generator
    ):
        """Test count with messages in inbox."""
        # Create some messages
        for i in range(5):
            msg = message_generator.generate_text_message()
            (inbox_dir / f"{msg['id']}.json").write_text('{}')

        with patch("src.daemon.daemon.INBOX_DIR", inbox_dir):
            from src.daemon.daemon import count_inbox_messages

            count = count_inbox_messages()
            assert count == 5

    def test_get_inbox_messages_empty(self, inbox_dir: Path):
        """Test get messages with empty inbox."""
        with patch("src.daemon.daemon.INBOX_DIR", inbox_dir):
            from src.daemon.daemon import get_inbox_messages

            messages = get_inbox_messages()
            assert messages == []

    def test_get_inbox_messages_returns_content(
        self, inbox_dir: Path, message_generator
    ):
        """Test get messages returns message content."""
        import json

        msg = message_generator.generate_text_message(text="Test message")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch("src.daemon.daemon.INBOX_DIR", inbox_dir):
            from src.daemon.daemon import get_inbox_messages

            messages = get_inbox_messages()

            assert len(messages) == 1
            assert messages[0]["text"] == "Test message"
