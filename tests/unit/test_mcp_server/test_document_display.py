"""
Tests for document/file message display in check_inbox.

Verifies that when a user sends a document (PDF, etc.) via Telegram,
the inbox server surfaces the local file path so the dispatcher can read it.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


class TestDocumentMessageDisplay:
    """Tests that document messages surface file_path in check_inbox output."""

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        return inbox

    def _make_document_message(self, msg_id="doc_123", file_name="ONBOARDING.pdf",
                                mime_type="application/pdf", file_path=None,
                                caption="", file_size=1024000):
        """Create a document message dict as the bot would produce."""
        if file_path is None:
            file_path = f"/home/ec2-user/messages/files/{msg_id}.pdf"
        text = caption if caption else f"[Document: {file_name}]"
        return {
            "id": msg_id,
            "source": "telegram",
            "type": "document",
            "chat_id": 123456,
            "user_id": 123456,
            "username": "testuser",
            "user_name": "TestUser",
            "text": text,
            "file_path": file_path,
            "file_name": file_name,
            "mime_type": mime_type,
            "file_size": file_size,
            "file_id": "BQACAgIAAxkBAAI",
            "timestamp": "2026-03-09T12:00:00",
        }

    def test_document_file_path_surfaced(self, setup_dirs):
        """Test that document messages show the file path in check_inbox output."""
        inbox = setup_dirs
        msg = self._make_document_message()
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))
            output = result[0].text

            # The file path must appear in the output
            assert "/home/ec2-user/messages/files/doc_123.pdf" in output

    def test_document_file_name_surfaced(self, setup_dirs):
        """Test that document messages show the original file name."""
        inbox = setup_dirs
        msg = self._make_document_message(file_name="report.pdf")
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))
            output = result[0].text

            assert "report.pdf" in output

    def test_document_shows_file_icon(self, setup_dirs):
        """Test that document messages show a file icon indicator."""
        inbox = setup_dirs
        msg = self._make_document_message()
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))
            output = result[0].text

            # Should have a document indicator (📎 or 📄)
            assert "📎" in output or "📄" in output

    def test_document_with_caption(self, setup_dirs):
        """Test that document messages with captions display the caption."""
        inbox = setup_dirs
        msg = self._make_document_message(caption="Here's the onboarding doc")
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))
            output = result[0].text

            assert "Here's the onboarding doc" in output
            assert msg["file_path"] in output

    def test_document_without_file_path_still_works(self, setup_dirs):
        """Test graceful handling when file_path is missing (shouldn't happen but be safe)."""
        inbox = setup_dirs
        msg = self._make_document_message()
        del msg["file_path"]  # Remove file_path
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))
            output = result[0].text

            # Should not crash, should still show the message
            assert "TestUser" in output
            assert "ONBOARDING.pdf" in output

    def test_pdf_document_hints_read_tool(self, setup_dirs):
        """Test that PDF documents hint that the Read tool can be used."""
        inbox = setup_dirs
        msg = self._make_document_message(mime_type="application/pdf")
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))
            output = result[0].text

            # Should hint that the file can be read
            assert "read" in output.lower() or "Read" in output

    def test_non_pdf_document_surfaced(self, setup_dirs):
        """Test that non-PDF documents (e.g., .xlsx, .txt) also surface file path."""
        inbox = setup_dirs
        msg = self._make_document_message(
            file_name="data.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            file_path="/home/ec2-user/messages/files/doc_123.xlsx",
        )
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
        ):
            import asyncio
            from src.mcp.inbox_server import handle_check_inbox

            result = asyncio.run(handle_check_inbox({}))
            output = result[0].text

            assert "/home/ec2-user/messages/files/doc_123.xlsx" in output
            assert "data.xlsx" in output
