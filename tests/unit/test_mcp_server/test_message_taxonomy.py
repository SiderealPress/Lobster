"""
Tests for the formal message type taxonomy (issue #156).

These tests import from src.mcp.message_types directly — a dependency-free
module — so they run without loading the full inbox_server stack.

The handle_mark_processing validation tests use the same patch.multiple pattern
as the existing test_message_state.py tests. Those tests share the pre-existing
environment constraint (require Docker or the full dev stack) documented in
tests/docker/Dockerfile.test.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/mcp is on the path so message_types.py can be imported directly.
_MCP_DIR = str(Path(__file__).resolve().parents[3] / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)


class TestTaxonomyConstants:
    """
    Verify that INBOX_MESSAGE_TYPES, INBOX_MESSAGE_SOURCES, and USER_FACING_TYPES
    are correctly defined in message_types.py (the dependency-free taxonomy module).
    """

    def test_inbox_message_types_is_frozenset(self):
        """INBOX_MESSAGE_TYPES must be a frozenset — immutable and hashable."""
        from message_types import INBOX_MESSAGE_TYPES
        assert isinstance(INBOX_MESSAGE_TYPES, frozenset)

    def test_inbox_message_sources_is_frozenset(self):
        """INBOX_MESSAGE_SOURCES must be a frozenset — immutable and hashable."""
        from message_types import INBOX_MESSAGE_SOURCES
        assert isinstance(INBOX_MESSAGE_SOURCES, frozenset)

    def test_user_facing_types_is_frozenset(self):
        """USER_FACING_TYPES must be a frozenset."""
        from message_types import USER_FACING_TYPES
        assert isinstance(USER_FACING_TYPES, frozenset)

    def test_user_facing_types_subset_of_inbox_message_types(self):
        """Every USER_FACING_TYPES entry must appear in INBOX_MESSAGE_TYPES."""
        from message_types import USER_FACING_TYPES, INBOX_MESSAGE_TYPES
        extras = USER_FACING_TYPES - INBOX_MESSAGE_TYPES
        assert not extras, (
            f"USER_FACING_TYPES contains types not in INBOX_MESSAGE_TYPES: {extras}"
        )

    def test_inbox_user_types_subset_of_inbox_message_types(self):
        """INBOX_USER_TYPES must be a subset of INBOX_MESSAGE_TYPES."""
        from message_types import INBOX_USER_TYPES, INBOX_MESSAGE_TYPES
        assert INBOX_USER_TYPES <= INBOX_MESSAGE_TYPES

    def test_inbox_system_types_subset_of_inbox_message_types(self):
        """INBOX_SYSTEM_TYPES must be a subset of INBOX_MESSAGE_TYPES."""
        from message_types import INBOX_SYSTEM_TYPES, INBOX_MESSAGE_TYPES
        assert INBOX_SYSTEM_TYPES <= INBOX_MESSAGE_TYPES

    def test_user_and_system_types_cover_all_message_types(self):
        """INBOX_MESSAGE_TYPES must equal INBOX_USER_TYPES | INBOX_SYSTEM_TYPES."""
        from message_types import INBOX_USER_TYPES, INBOX_SYSTEM_TYPES, INBOX_MESSAGE_TYPES
        assert INBOX_MESSAGE_TYPES == INBOX_USER_TYPES | INBOX_SYSTEM_TYPES

    def test_required_user_types_present(self):
        """Core user-initiated types from the RFC must be in INBOX_MESSAGE_TYPES."""
        from message_types import INBOX_MESSAGE_TYPES
        required = {"text", "voice", "photo", "document", "callback"}
        missing = required - INBOX_MESSAGE_TYPES
        assert not missing, f"Required user types missing: {missing}"

    def test_required_system_types_present(self):
        """Core system types from the RFC must be in INBOX_MESSAGE_TYPES."""
        from message_types import INBOX_MESSAGE_TYPES
        required = {
            "self_check",
            "subagent_result",
            "subagent_error",
            "subagent_notification",
            "subagent_observation",
            "subagent_stale_check",
        }
        missing = required - INBOX_MESSAGE_TYPES
        assert not missing, f"Required system types missing: {missing}"

    def test_required_sources_present(self):
        """All known message sources must be in INBOX_MESSAGE_SOURCES."""
        from message_types import INBOX_MESSAGE_SOURCES
        required = {"telegram", "slack", "sms", "signal", "whatsapp", "bisque", "system", "gmail"}
        missing = required - INBOX_MESSAGE_SOURCES
        assert not missing, f"Required sources missing: {missing}"

    def test_user_facing_types_excludes_system_types(self):
        """USER_FACING_TYPES must not contain any pure system types."""
        from message_types import USER_FACING_TYPES, INBOX_SYSTEM_TYPES
        overlap = USER_FACING_TYPES & INBOX_SYSTEM_TYPES
        assert not overlap, (
            f"USER_FACING_TYPES must not include system types, but found: {overlap}"
        )

    def test_no_empty_strings_in_types(self):
        """No taxonomy entry should be an empty string."""
        from message_types import INBOX_MESSAGE_TYPES, INBOX_MESSAGE_SOURCES
        assert "" not in INBOX_MESSAGE_TYPES, "Empty string found in INBOX_MESSAGE_TYPES"
        assert "" not in INBOX_MESSAGE_SOURCES, "Empty string found in INBOX_MESSAGE_SOURCES"

    def test_no_whitespace_in_type_values(self):
        """Type and source values must not contain whitespace (routing keys)."""
        from message_types import INBOX_MESSAGE_TYPES, INBOX_MESSAGE_SOURCES
        bad_types = {t for t in INBOX_MESSAGE_TYPES if " " in t or "\t" in t}
        bad_sources = {s for s in INBOX_MESSAGE_SOURCES if " " in s or "\t" in s}
        assert not bad_types, f"Types with whitespace: {bad_types}"
        assert not bad_sources, f"Sources with whitespace: {bad_sources}"


class TestMarkProcessingTypeValidation:
    """
    Verify that handle_mark_processing logs a warning for unknown types/sources.

    These tests use the same patch.multiple("src.mcp.inbox_server", ...) pattern
    as the existing test_message_state.py tests and share the same environment
    constraint: they require the full dev stack (Docker or local install) to run
    because inbox_server.py imports heavy dependencies (reliability, watchdog,
    sqlite, etc.) that are not available in the base test venv.
    """

    @pytest.fixture
    def setup_dirs(self, temp_messages_dir: Path):
        inbox = temp_messages_dir / "inbox"
        processing = temp_messages_dir / "processing"
        return inbox, processing

    def test_unknown_type_logs_warning(self, setup_dirs, caplog):
        """mark_processing must emit a WARNING for a type not in INBOX_MESSAGE_TYPES."""
        inbox, processing = setup_dirs
        msg_id = "1234567890_test_unknown_type"
        msg = {
            "id": msg_id,
            "type": "totally_unknown_type_xyz",
            "source": "telegram",
            "text": "hello",
        }
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import handle_mark_processing
            with caplog.at_level(logging.WARNING, logger="inbox_server"):
                asyncio.run(handle_mark_processing({"message_id": msg_id}))

        assert any(
            "unknown message type" in r.message and "totally_unknown_type_xyz" in r.message
            for r in caplog.records
        ), f"Expected WARNING about unknown type, got: {[r.message for r in caplog.records]}"

    def test_unknown_source_logs_warning(self, setup_dirs, caplog):
        """mark_processing must emit a WARNING for a source not in INBOX_MESSAGE_SOURCES."""
        inbox, processing = setup_dirs
        msg_id = "1234567890_test_unknown_source"
        msg = {
            "id": msg_id,
            "type": "text",
            "source": "totally_unknown_source_xyz",
            "text": "hello",
        }
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import handle_mark_processing
            with caplog.at_level(logging.WARNING, logger="inbox_server"):
                asyncio.run(handle_mark_processing({"message_id": msg_id}))

        assert any(
            "unknown message source" in r.message and "totally_unknown_source_xyz" in r.message
            for r in caplog.records
        ), f"Expected WARNING about unknown source, got: {[r.message for r in caplog.records]}"

    def test_unknown_type_does_not_block_processing(self, setup_dirs):
        """mark_processing must succeed (not raise) for unknown types — non-blocking validation."""
        inbox, processing = setup_dirs
        msg_id = "1234567890_test_unknown_noblocking"
        msg = {
            "id": msg_id,
            "type": "some_future_type_not_yet_registered",
            "source": "telegram",
            "text": "hello",
        }
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import handle_mark_processing
            result = asyncio.run(handle_mark_processing({"message_id": msg_id}))

        assert "claimed" in result[0].text.lower()
        assert not (inbox / f"{msg_id}.json").exists()
        assert (processing / f"{msg_id}.json").exists()

    def test_known_type_no_warning(self, setup_dirs, caplog):
        """mark_processing must NOT emit a type warning for known types."""
        inbox, processing = setup_dirs
        msg_id = "1234567890_test_known_type"
        msg = {
            "id": msg_id,
            "type": "text",
            "source": "telegram",
            "text": "hello",
        }
        (inbox / f"{msg_id}.json").write_text(json.dumps(msg))

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox,
            PROCESSING_DIR=processing,
        ):
            from src.mcp.inbox_server import handle_mark_processing
            with caplog.at_level(logging.WARNING, logger="inbox_server"):
                asyncio.run(handle_mark_processing({"message_id": msg_id}))

        type_warnings = [
            r for r in caplog.records
            if "unknown message type" in r.message
        ]
        assert not type_warnings, f"Unexpected type warning for known type 'text': {type_warnings}"
