"""
tests/unit/test_channels.py — Unit tests for src/channels/ (BIS-159 Slice 5).

Tests cover:
  - ChannelAdapter Protocol structural subtyping
  - OutboxFileHandler.write() happy path
  - OutboxFileHandler.write() missing id raises KeyError
  - OutboxFileHandler.write() creates parent directory if absent
  - Atomic write guarantees (temp + rename)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from channels import ChannelAdapter, OutboxFileHandler
from channels.base import ChannelAdapter as ChannelAdapterBase


class TestChannelAdapterProtocol:
    def test_outbox_file_handler_satisfies_protocol(self, tmp_path):
        handler = OutboxFileHandler(outbox_dir=tmp_path)
        # isinstance check works because ChannelAdapter is @runtime_checkable
        assert isinstance(handler, ChannelAdapter)

    def test_arbitrary_class_with_write_satisfies_protocol(self):
        class MockAdapter:
            def write(self, reply: dict) -> None:
                pass

        assert isinstance(MockAdapter(), ChannelAdapter)

    def test_object_without_write_does_not_satisfy_protocol(self):
        class NoWrite:
            pass

        assert not isinstance(NoWrite(), ChannelAdapter)


class TestOutboxFileHandler:
    def test_write_creates_json_file(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        handler = OutboxFileHandler(outbox_dir=outbox)

        reply = {"id": "msg-001", "chat_id": 123, "text": "Hello"}
        handler.write(reply)

        expected = outbox / "msg-001.json"
        assert expected.exists()
        data = json.loads(expected.read_text())
        assert data["text"] == "Hello"

    def test_write_missing_id_raises_key_error(self, tmp_path):
        handler = OutboxFileHandler(outbox_dir=tmp_path)
        with pytest.raises(KeyError):
            handler.write({"chat_id": 123, "text": "No ID"})

    def test_write_creates_parent_directory(self, tmp_path):
        outbox = tmp_path / "deep" / "outbox"
        # Directory does not exist yet
        assert not outbox.exists()
        handler = OutboxFileHandler(outbox_dir=outbox)
        handler.write({"id": "x", "text": "hi"})
        assert (outbox / "x.json").exists()

    def test_outbox_dir_property(self, tmp_path):
        handler = OutboxFileHandler(outbox_dir=tmp_path)
        assert handler.outbox_dir == tmp_path

    def test_repr_contains_path(self, tmp_path):
        handler = OutboxFileHandler(outbox_dir=tmp_path)
        assert str(tmp_path) in repr(handler)

    def test_write_is_atomic_via_atomic_write_json(self, tmp_path):
        """OutboxFileHandler.write must delegate to atomic_write_json."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        handler = OutboxFileHandler(outbox_dir=outbox)

        reply = {"id": "atomic-test", "chat_id": 1, "text": "safe"}

        with patch("channels.outbox.atomic_write_json") as mock_atomic:
            handler.write(reply)
            mock_atomic.assert_called_once_with(
                outbox / "atomic-test.json", reply
            )

    def test_write_idempotent_on_duplicate_id(self, tmp_path):
        """Writing the same id twice overwrites the file (atomic_write_json semantics)."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        handler = OutboxFileHandler(outbox_dir=outbox)

        handler.write({"id": "dup", "text": "first"})
        handler.write({"id": "dup", "text": "second"})

        data = json.loads((outbox / "dup.json").read_text())
        assert data["text"] == "second"
