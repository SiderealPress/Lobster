"""Tests for bisque Wire Protocol v2 frame types, serialization, and validation."""

from __future__ import annotations

import json
import uuid

import pytest

from bisque.protocol import (
    CLIENT_FRAME_TYPES,
    SERVER_FRAME_TYPES,
    Envelope,
    FrameType,
    ProtocolError,
    StatusValue,
    deserialize,
    frame_agent_completed,
    frame_agent_started,
    frame_auth_error,
    frame_auth_success,
    frame_error,
    frame_inbox_update,
    frame_message,
    frame_pong,
    frame_snapshot,
    frame_status,
    frame_stream_delta,
    frame_stream_end,
    frame_stream_start,
    frame_tool_call,
    frame_tool_result,
    make_envelope,
    serialize,
    validate_client_frame,
)


# =============================================================================
# Envelope basics
# =============================================================================


class TestEnvelope:
    def test_envelope_is_frozen(self):
        env = make_envelope(FrameType.PONG)
        with pytest.raises(AttributeError):
            env.v = 3

    def test_make_envelope_auto_generates_id(self):
        env = make_envelope(FrameType.PONG)
        uuid.UUID(env.id)  # should not raise

    def test_make_envelope_auto_generates_ts(self):
        env = make_envelope(FrameType.PONG)
        assert "T" in env.ts  # ISO-8601

    def test_make_envelope_version_is_2(self):
        env = make_envelope(FrameType.PONG)
        assert env.v == 2

    def test_make_envelope_stores_payload(self):
        env = make_envelope(FrameType.MESSAGE, text="hello", role="user")
        assert env.payload == {"text": "hello", "role": "user"}

    def test_make_envelope_accepts_string_type(self):
        env = make_envelope("pong")
        assert env.type == "pong"


# =============================================================================
# FrameType enum
# =============================================================================


class TestFrameType:
    def test_all_19_frame_types(self):
        assert len(FrameType) == 19

    def test_client_types(self):
        for t in ["auth", "send_message", "ack", "ping"]:
            assert t in CLIENT_FRAME_TYPES

    def test_server_types(self):
        for t in [
            "auth_success", "auth_error", "snapshot", "message",
            "inbox_update", "status", "tool_call", "tool_result",
            "stream_start", "stream_delta", "stream_end",
            "agent_started", "agent_completed", "error", "pong",
        ]:
            assert t in SERVER_FRAME_TYPES

    def test_client_server_disjoint(self):
        assert CLIENT_FRAME_TYPES & SERVER_FRAME_TYPES == set()


# =============================================================================
# StatusValue enum
# =============================================================================


class TestStatusValue:
    def test_status_values(self):
        assert StatusValue.IDLE.value == "idle"
        assert StatusValue.THINKING.value == "thinking"
        assert StatusValue.EXECUTING.value == "executing"
        assert StatusValue.WAITING.value == "waiting"


# =============================================================================
# Serialization round-trips
# =============================================================================


class TestSerialization:
    def test_serialize_flat_format(self):
        env = make_envelope(FrameType.MESSAGE, text="hi", role="user")
        raw = serialize(env)
        data = json.loads(raw)
        assert data["v"] == 2
        assert data["type"] == "message"
        assert data["text"] == "hi"
        assert data["role"] == "user"
        assert "payload" not in data  # payload merged, not nested

    def test_deserialize_round_trip(self):
        env = make_envelope(FrameType.PONG)
        raw = serialize(env)
        env2 = deserialize(raw)
        assert env2.v == env.v
        assert env2.id == env.id
        assert env2.type == env.type

    def test_deserialize_invalid_json(self):
        with pytest.raises(ProtocolError):
            deserialize("not json {")

    def test_deserialize_missing_v(self):
        raw = json.dumps({"type": "pong", "id": "x", "ts": "t"})
        with pytest.raises(ProtocolError):
            deserialize(raw)

    def test_deserialize_missing_type(self):
        raw = json.dumps({"v": 2, "id": "x", "ts": "t"})
        with pytest.raises(ProtocolError):
            deserialize(raw)

    def test_deserialize_extracts_payload(self):
        data = {"v": 2, "id": "abc", "ts": "2025-01-01T00:00:00Z", "type": "message", "text": "hi", "role": "user"}
        env = deserialize(json.dumps(data))
        assert env.payload["text"] == "hi"
        assert env.payload["role"] == "user"

    def test_serialize_unicode(self):
        env = make_envelope(FrameType.MESSAGE, text="hello 🦞", role="user")
        raw = serialize(env)
        data = json.loads(raw)
        assert data["text"] == "hello 🦞"

    def test_serialize_empty_payload(self):
        env = make_envelope(FrameType.PONG)
        raw = serialize(env)
        data = json.loads(raw)
        assert data["type"] == "pong"

    def test_serialize_empty_lists(self):
        env = make_envelope(FrameType.SNAPSHOT, recent_messages=[], tasks=[])
        raw = serialize(env)
        data = json.loads(raw)
        assert data["recent_messages"] == []
        assert data["tasks"] == []


# =============================================================================
# Client frame validation
# =============================================================================


class TestValidateClientFrame:
    def test_auth_valid(self):
        env = make_envelope(FrameType.AUTH, token="abc123")
        validate_client_frame(env)  # should not raise

    def test_auth_missing_token(self):
        env = make_envelope(FrameType.AUTH)
        with pytest.raises(ProtocolError):
            validate_client_frame(env)

    def test_send_message_valid(self):
        env = make_envelope(FrameType.SEND_MESSAGE, text="hello")
        validate_client_frame(env)

    def test_send_message_missing_text(self):
        env = make_envelope(FrameType.SEND_MESSAGE)
        with pytest.raises(ProtocolError):
            validate_client_frame(env)

    def test_ack_valid(self):
        env = make_envelope(FrameType.ACK, event_id="evt-123")
        validate_client_frame(env)

    def test_ack_missing_event_id(self):
        env = make_envelope(FrameType.ACK)
        with pytest.raises(ProtocolError):
            validate_client_frame(env)

    def test_ping_valid(self):
        env = make_envelope(FrameType.PING)
        validate_client_frame(env)

    def test_server_type_rejected(self):
        env = make_envelope(FrameType.PONG)
        with pytest.raises(ProtocolError):
            validate_client_frame(env)

    def test_unknown_type_rejected(self):
        env = make_envelope("nonexistent")
        with pytest.raises(ProtocolError):
            validate_client_frame(env)


# =============================================================================
# Frame builders — each returns serialized JSON string
# =============================================================================


class TestFrameBuilders:
    def test_frame_auth_success(self):
        raw = frame_auth_success("user@test.com")
        data = json.loads(raw)
        assert data["type"] == "auth_success"
        assert data["email"] == "user@test.com"
        assert data["v"] == 2

    def test_frame_auth_error(self):
        raw = frame_auth_error("bad token", code=4401)
        data = json.loads(raw)
        assert data["type"] == "auth_error"
        assert data["message"] == "bad token"
        assert data["code"] == 4401

    def test_frame_snapshot(self):
        raw = frame_snapshot("idle", recent_messages=[{"text": "hi"}], tasks=[], last_event_id="evt-1")
        data = json.loads(raw)
        assert data["type"] == "snapshot"
        assert data["status"] == "idle"
        assert data["recent_messages"] == [{"text": "hi"}]
        assert data["last_event_id"] == "evt-1"

    def test_frame_snapshot_defaults(self):
        raw = frame_snapshot("thinking")
        data = json.loads(raw)
        assert data["status"] == "thinking"
        assert data.get("recent_messages") is None or data["recent_messages"] is None

    def test_frame_message(self):
        raw = frame_message("hello", "assistant", source="bisque", chat_id="u@t.com", msg_id="m1")
        data = json.loads(raw)
        assert data["type"] == "message"
        assert data["text"] == "hello"
        assert data["role"] == "assistant"
        assert data["source"] == "bisque"
        assert data["msg_id"] == "m1"

    def test_frame_inbox_update(self):
        raw = frame_inbox_update("received", "msg-123", preview="Hello...")
        data = json.loads(raw)
        assert data["type"] == "inbox_update"
        assert data["action"] == "received"
        assert data["message_id"] == "msg-123"

    def test_frame_status(self):
        raw = frame_status("thinking", detail="Processing query")
        data = json.loads(raw)
        assert data["type"] == "status"
        assert data["status"] == "thinking"
        assert data["detail"] == "Processing query"

    def test_frame_tool_call(self):
        raw = frame_tool_call("read_file", arguments={"path": "/tmp/x"})
        data = json.loads(raw)
        assert data["type"] == "tool_call"
        assert data["tool_name"] == "read_file"
        assert data["arguments"]["path"] == "/tmp/x"

    def test_frame_tool_result(self):
        raw = frame_tool_result("read_file", result="contents here")
        data = json.loads(raw)
        assert data["type"] == "tool_result"
        assert data["tool_name"] == "read_file"

    def test_frame_stream_start(self):
        raw = frame_stream_start(stream_id="s1")
        data = json.loads(raw)
        assert data["type"] == "stream_start"
        assert data["stream_id"] == "s1"

    def test_frame_stream_delta(self):
        raw = frame_stream_delta("chunk of text", stream_id="s1")
        data = json.loads(raw)
        assert data["type"] == "stream_delta"
        assert data["text"] == "chunk of text"

    def test_frame_stream_end(self):
        raw = frame_stream_end(stream_id="s1")
        data = json.loads(raw)
        assert data["type"] == "stream_end"

    def test_frame_agent_started(self):
        raw = frame_agent_started(task="research")
        data = json.loads(raw)
        assert data["type"] == "agent_started"
        assert data["task"] == "research"

    def test_frame_agent_completed(self):
        raw = frame_agent_completed(task="research", result="done")
        data = json.loads(raw)
        assert data["type"] == "agent_completed"

    def test_frame_error(self):
        raw = frame_error("something broke", code=500)
        data = json.loads(raw)
        assert data["type"] == "error"
        assert data["message"] == "something broke"
        assert data["code"] == 500

    def test_frame_pong(self):
        raw = frame_pong()
        data = json.loads(raw)
        assert data["type"] == "pong"
        assert data["v"] == 2


# =============================================================================
# ProtocolError
# =============================================================================


class TestProtocolError:
    def test_protocol_error_message(self):
        err = ProtocolError("bad frame")
        assert str(err) == "bad frame"

    def test_protocol_error_code(self):
        err = ProtocolError("bad frame", code=4400)
        assert err.code == 4400

    def test_protocol_error_default_code(self):
        err = ProtocolError("bad frame")
        assert err.code is None
