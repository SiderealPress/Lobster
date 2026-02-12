"""
Tests for reliability utilities (atomic writes, validation, audit logging, etc.)

These tests verify the core reliability primitives that protect against
common agent failure patterns.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Add src to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from reliability import (
    atomic_write_json,
    safe_move,
    validate_send_reply_args,
    validate_message_id,
    ValidationError,
    init_audit_log,
    audit_log,
    IdempotencyTracker,
    CircuitBreaker,
)


# =============================================================================
# Atomic Write Tests
# =============================================================================


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path):
        """Atomic write produces valid, readable JSON."""
        path = tmp_path / "test.json"
        data = {"key": "value", "number": 42}
        atomic_write_json(path, data)

        result = json.loads(path.read_text())
        assert result == data

    def test_overwrites_existing_file(self, tmp_path):
        """Atomic write correctly replaces an existing file."""
        path = tmp_path / "test.json"
        atomic_write_json(path, {"old": True})
        atomic_write_json(path, {"new": True})

        result = json.loads(path.read_text())
        assert result == {"new": True}

    def test_no_temp_files_left_on_success(self, tmp_path):
        """Successful write cleans up temp files."""
        path = tmp_path / "test.json"
        atomic_write_json(path, {"clean": True})

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "test.json"

    def test_fails_on_unserializable_data(self, tmp_path):
        """Non-serializable data raises TypeError, not a corrupt file."""
        path = tmp_path / "test.json"
        with pytest.raises(TypeError):
            atomic_write_json(path, {"bad": object()})

        # File should not exist (failed before rename)
        assert not path.exists()

    def test_no_temp_files_left_on_failure(self, tmp_path):
        """Failed write cleans up temp files."""
        path = tmp_path / "test.json"
        try:
            atomic_write_json(path, {"bad": object()})
        except TypeError:
            pass

        # No temp files left behind
        tmp_files = [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]
        assert len(tmp_files) == 0


class TestSafeMove:
    def test_moves_file(self, tmp_path):
        """safe_move moves an existing file."""
        src = tmp_path / "src.json"
        dest = tmp_path / "dest.json"
        src.write_text('{"test": true}')

        result = safe_move(src, dest)

        assert result is True
        assert not src.exists()
        assert dest.exists()

    def test_idempotent_on_missing_source(self, tmp_path):
        """safe_move returns False (not error) if source is already gone."""
        src = tmp_path / "gone.json"
        dest = tmp_path / "dest.json"

        result = safe_move(src, dest)
        assert result is False


# =============================================================================
# Validation Tests
# =============================================================================


class TestValidateSendReplyArgs:
    def test_valid_telegram_args(self):
        """Valid Telegram reply args pass validation."""
        args = {"chat_id": 12345, "text": "Hello"}
        result = validate_send_reply_args(args)
        assert result["chat_id"] == 12345
        assert result["text"] == "Hello"
        assert result["source"] == "telegram"

    def test_valid_slack_args(self):
        """Valid Slack reply args pass validation."""
        args = {"chat_id": "C01ABC123", "text": "Hello", "source": "slack"}
        result = validate_send_reply_args(args)
        assert result["chat_id"] == "C01ABC123"
        assert result["source"] == "slack"

    def test_missing_chat_id_raises(self):
        """Missing chat_id raises ValidationError."""
        with pytest.raises(ValidationError, match="chat_id"):
            validate_send_reply_args({"text": "Hello"})

    def test_empty_text_raises(self):
        """Empty text raises ValidationError."""
        with pytest.raises(ValidationError, match="text"):
            validate_send_reply_args({"chat_id": 123, "text": ""})

    def test_invalid_source_raises(self):
        """Unknown source raises ValidationError."""
        with pytest.raises(ValidationError, match="Invalid source"):
            validate_send_reply_args({"chat_id": 123, "text": "Hi", "source": "carrier_pigeon"})

    def test_truncates_long_text(self):
        """Text longer than 4096 chars is truncated."""
        long_text = "x" * 5000
        result = validate_send_reply_args({"chat_id": 123, "text": long_text})
        assert len(result["text"]) == 4096
        assert result["text"].endswith("...")

    def test_float_chat_id_converted(self):
        """Float chat_id (common LLM error) is converted to int."""
        result = validate_send_reply_args({"chat_id": 123.0, "text": "Hi"})
        assert result["chat_id"] == 123
        assert isinstance(result["chat_id"], int)


class TestValidateMessageId:
    def test_valid_id(self):
        """Valid message ID passes."""
        assert validate_message_id("1234567890_123") == "1234567890_123"

    def test_empty_raises(self):
        """Empty message ID raises ValidationError."""
        with pytest.raises(ValidationError, match="required"):
            validate_message_id("")

    def test_none_raises(self):
        """None message ID raises ValidationError."""
        with pytest.raises(ValidationError, match="required"):
            validate_message_id(None)

    def test_path_traversal_rejected(self):
        """Path traversal attempts are rejected."""
        with pytest.raises(ValidationError, match="invalid characters"):
            validate_message_id("../../etc/passwd")

    def test_slash_rejected(self):
        """Slashes in message ID are rejected."""
        with pytest.raises(ValidationError, match="invalid characters"):
            validate_message_id("foo/bar")


# =============================================================================
# Audit Log Tests
# =============================================================================


class TestAuditLog:
    def test_writes_jsonl(self, tmp_path):
        """Audit log produces valid JSONL entries."""
        init_audit_log(tmp_path)
        audit_log(tool="test_tool", args={"key": "value"}, result="ok")

        log_file = tmp_path / "audit.jsonl"
        assert log_file.exists()

        line = log_file.read_text().strip()
        entry = json.loads(line)
        assert entry["tool"] == "test_tool"
        assert entry["args"]["key"] == "value"
        assert entry["result"] == "ok"
        assert "ts" in entry

    def test_truncates_long_text(self, tmp_path):
        """Long text fields are truncated in audit log."""
        init_audit_log(tmp_path)
        audit_log(tool="send_reply", args={"text": "x" * 1000})

        log_file = tmp_path / "audit.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert len(entry["args"]["text"]) <= 203  # 200 + "..."

    def test_redacts_secrets(self, tmp_path):
        """Sensitive fields are redacted."""
        init_audit_log(tmp_path)
        audit_log(tool="test", args={"api_key": "sk-secret123"})

        log_file = tmp_path / "audit.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert entry["args"]["api_key"] == "[REDACTED]"

    def test_append_only(self, tmp_path):
        """Multiple audit entries append, never overwrite."""
        init_audit_log(tmp_path)
        audit_log(tool="first")
        audit_log(tool="second")

        log_file = tmp_path / "audit.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["tool"] == "first"
        assert json.loads(lines[1])["tool"] == "second"


# =============================================================================
# Idempotency Tracker Tests
# =============================================================================


class TestIdempotencyTracker:
    def test_first_check_returns_true(self):
        """First time seeing an ID returns True (new)."""
        tracker = IdempotencyTracker(ttl_seconds=60)
        assert tracker.check_and_mark("msg_001") is True

    def test_duplicate_returns_false(self):
        """Second time seeing same ID returns False (duplicate)."""
        tracker = IdempotencyTracker(ttl_seconds=60)
        tracker.check_and_mark("msg_001")
        assert tracker.check_and_mark("msg_001") is False

    def test_different_ids_both_new(self):
        """Different IDs are tracked independently."""
        tracker = IdempotencyTracker(ttl_seconds=60)
        assert tracker.check_and_mark("msg_001") is True
        assert tracker.check_and_mark("msg_002") is True

    def test_expired_entries_cleaned(self):
        """Entries older than TTL are evicted."""
        tracker = IdempotencyTracker(ttl_seconds=1)
        tracker.check_and_mark("msg_001")
        time.sleep(1.1)
        # Should be treated as new after TTL expires
        assert tracker.check_and_mark("msg_001") is True


# =============================================================================
# Circuit Breaker Tests
# =============================================================================


class TestCircuitBreaker:
    def test_starts_closed(self):
        """Circuit breaker starts in closed (allowing) state."""
        cb = CircuitBreaker("test", failure_threshold=3)
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_opens_after_threshold(self):
        """Circuit opens after N consecutive failures."""
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.allow_request() is True  # Still under threshold

        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.allow_request() is False

    def test_success_resets_count(self):
        """A success resets the failure counter."""
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # Reset
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED

    def test_half_open_after_cooldown(self):
        """Circuit moves to half-open after cooldown period."""
        cb = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=1)
        cb.record_failure()
        # Immediately after failure with cooldown=1s, should be open
        assert cb.allow_request() is False

        time.sleep(1.1)
        # After cooldown, should be half-open (allows one test request)
        assert cb.state == CircuitBreaker.HALF_OPEN
        assert cb.allow_request() is True

    def test_status_returns_dict(self):
        """status() returns a dictionary with breaker metadata."""
        cb = CircuitBreaker("telegram", failure_threshold=5, cooldown_seconds=120)
        status = cb.status()
        assert status["name"] == "telegram"
        assert status["state"] == "closed"
        assert status["failure_threshold"] == 5
