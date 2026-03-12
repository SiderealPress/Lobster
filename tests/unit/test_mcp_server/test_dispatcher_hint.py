"""
Tests for dispatcher_hint in check_inbox / wait_for_messages

Verifies that messages with file attachments (voice, photo, document) get a
plain-ASCII dispatcher_hint line, and that plain text messages do not.
"""

import asyncio
import json
import sys
from pathlib import Path
import pytest
from unittest.mock import patch

# inbox_server imports sibling modules (reliability, update_manager, etc.) from
# src/mcp/, so we must add that directory to sys.path before importing it.
_MCP_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

# Pre-import so patch.multiple("src.mcp.inbox_server", ...) can resolve the target.
import src.mcp.inbox_server  # noqa: E402


class TestDispatcherHint:
    """Unit tests for dispatcher_hint feature."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def _check_inbox(self, inbox_dir: Path) -> str:
        """Helper: run handle_check_inbox and return the result text."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
        ):
            from src.mcp.inbox_server import handle_check_inbox
            result = asyncio.run(handle_check_inbox({}))
            return result[0].text

    # ------------------------------------------------------------------
    # Hint PRESENT for file-bearing messages
    # ------------------------------------------------------------------

    def test_voice_message_gets_hint(self, inbox_dir: Path, message_generator):
        """Voice messages should include the dispatcher_hint line."""
        msg = message_generator.generate_voice_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_photo_message_gets_hint(self, inbox_dir: Path, message_generator):
        """Photo messages should include the dispatcher_hint line."""
        msg = message_generator.generate_photo_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_photo_message_multi_image_gets_hint(self, inbox_dir: Path, message_generator):
        """Photo messages with multiple images should include the dispatcher_hint line."""
        msg = message_generator.generate_photo_message(
            image_files=["/tmp/photo_a.jpg", "/tmp/photo_b.jpg"]
        )
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_document_message_gets_hint(self, inbox_dir: Path, message_generator):
        """Document messages should include the dispatcher_hint line."""
        msg = message_generator.generate_document_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_text_message_with_image_file_field_gets_hint(
        self, inbox_dir: Path, message_generator
    ):
        """Text-typed messages that happen to have an image_file field get the hint."""
        msg = message_generator.generate_text_message()
        msg["image_file"] = "/tmp/images/some_image.jpg"
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_text_message_with_file_path_field_gets_hint(
        self, inbox_dir: Path, message_generator
    ):
        """Text-typed messages that happen to have a file_path field get the hint."""
        msg = message_generator.generate_text_message()
        msg["file_path"] = "/tmp/files/attachment.pdf"
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    def test_text_message_with_audio_file_field_gets_hint(
        self, inbox_dir: Path, message_generator
    ):
        """Text-typed messages that happen to have an audio_file field get the hint."""
        msg = message_generator.generate_text_message()
        msg["audio_file"] = "/tmp/audio/clip.ogg"
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint: HINT: file attached - use subagent" in text

    # ------------------------------------------------------------------
    # Hint ABSENT for plain messages
    # ------------------------------------------------------------------

    def test_plain_text_message_no_hint(self, inbox_dir: Path, message_generator):
        """Plain text messages must NOT include the dispatcher_hint line."""
        msg = message_generator.generate_text_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "dispatcher_hint" not in text

    # ------------------------------------------------------------------
    # Hint is plain ASCII -- no Unicode characters
    # ------------------------------------------------------------------

    def test_hint_contains_no_unicode(self, inbox_dir: Path, message_generator):
        """The dispatcher_hint value must be pure ASCII (no emoji or Unicode)."""
        msg = message_generator.generate_voice_message()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        # Extract the dispatcher_hint line
        hint_line = next(
            (line for line in text.splitlines() if line.startswith("dispatcher_hint:")),
            None,
        )
        assert hint_line is not None, "dispatcher_hint line not found"
        # Verify it's pure ASCII -- will raise UnicodeEncodeError if not
        hint_line.encode("ascii")

    # ------------------------------------------------------------------
    # Multiple messages in inbox
    # ------------------------------------------------------------------

    def test_mixed_inbox_hints_only_on_file_messages(
        self, inbox_dir: Path, message_generator
    ):
        """Only file-bearing messages get a hint; text messages do not."""
        text_msg = message_generator.generate_text_message()
        voice_msg = message_generator.generate_voice_message()
        (inbox_dir / f"{text_msg['id']}.json").write_text(json.dumps(text_msg))
        (inbox_dir / f"{voice_msg['id']}.json").write_text(json.dumps(voice_msg))

        text = self._check_inbox(inbox_dir)

        # Count hint occurrences -- exactly one (for the voice message)
        hint_count = text.count("dispatcher_hint: HINT: file attached - use subagent")
        assert hint_count == 1
