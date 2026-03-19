"""Bisque Wire Protocol v2 -- envelope framing, serialization, and frame builders."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """Immutable wire-protocol envelope (v2)."""

    v: int
    id: str
    ts: str
    type: str
    payload: Dict[str, Any]


# ---------------------------------------------------------------------------
# Frame types
# ---------------------------------------------------------------------------


class FrameType(str, Enum):
    """All 19 wire-protocol frame types."""

    # Client -> Server (4)
    AUTH = "auth"
    SEND_MESSAGE = "send_message"
    ACK = "ack"
    PING = "ping"

    # Server -> Client (16)
    HELLO = "hello"
    AUTH_SUCCESS = "auth_success"
    AUTH_ERROR = "auth_error"
    SNAPSHOT = "snapshot"
    MESSAGE = "message"
    INBOX_UPDATE = "inbox_update"
    STATUS = "status"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STREAM_START = "stream_start"
    STREAM_DELTA = "stream_delta"
    STREAM_END = "stream_end"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    ERROR = "error"
    PONG = "pong"


CLIENT_FRAME_TYPES: set[str] = {
    FrameType.AUTH.value,
    FrameType.SEND_MESSAGE.value,
    # "message" is accepted as a client-compat alias for "send_message"
    FrameType.MESSAGE.value,
    FrameType.ACK.value,
    FrameType.PING.value,
}

SERVER_FRAME_TYPES: set[str] = {
    FrameType.HELLO.value,
    FrameType.AUTH_SUCCESS.value,
    FrameType.AUTH_ERROR.value,
    FrameType.SNAPSHOT.value,
    FrameType.MESSAGE.value,
    FrameType.INBOX_UPDATE.value,
    FrameType.STATUS.value,
    FrameType.TOOL_CALL.value,
    FrameType.TOOL_RESULT.value,
    FrameType.STREAM_START.value,
    FrameType.STREAM_DELTA.value,
    FrameType.STREAM_END.value,
    FrameType.AGENT_STARTED.value,
    FrameType.AGENT_COMPLETED.value,
    FrameType.ERROR.value,
    FrameType.PONG.value,
}


# ---------------------------------------------------------------------------
# Status values
# ---------------------------------------------------------------------------


class StatusValue(str, Enum):
    """Possible values for the ``status`` frame."""

    IDLE = "idle"
    THINKING = "thinking"
    EXECUTING = "executing"
    WAITING = "waiting"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """Raised on wire-protocol violations."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def make_envelope(frame_type: str, **payload: Any) -> Envelope:
    """Create an :class:`Envelope` with auto-generated id and timestamp."""
    return Envelope(
        v=2,
        id=str(uuid.uuid4()),
        ts=datetime.now(timezone.utc).isoformat(),
        type=frame_type,
        payload=payload,
    )


def serialize(envelope: Envelope) -> str:
    """Serialize an :class:`Envelope` to a flat JSON string.

    The payload keys are merged into the top-level object alongside
    ``v``, ``id``, ``ts``, and ``type``.
    """
    data: Dict[str, Any] = {
        "v": envelope.v,
        "id": envelope.id,
        "ts": envelope.ts,
        "type": envelope.type,
    }
    data.update(envelope.payload)
    return json.dumps(data, separators=(",", ":"))


def deserialize(raw: str) -> Envelope:
    """Deserialize a JSON string into an :class:`Envelope`.

    Raises :class:`ProtocolError` on invalid JSON or missing required fields.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProtocolError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ProtocolError("Expected JSON object")

    if "type" not in data:
        raise ProtocolError("Missing required field: type")

    # Extract envelope-level fields; everything else is payload.
    # v is optional for client compat — default to 2 if absent.
    v = data.pop("v", 2)
    envelope_id = data.pop("id", str(uuid.uuid4()))
    ts = data.pop("ts", datetime.now(timezone.utc).isoformat())
    frame_type = data.pop("type")
    payload = data  # remaining keys

    return Envelope(v=v, id=envelope_id, ts=ts, type=frame_type, payload=payload)


# ---------------------------------------------------------------------------
# Client frame validation
# ---------------------------------------------------------------------------

_CLIENT_REQUIRED_FIELDS: Dict[str, List[str]] = {
    FrameType.AUTH.value: ["token"],
    FrameType.SEND_MESSAGE.value: ["text"],
    # "message" is a client-compat alias for "send_message" — same required fields
    FrameType.MESSAGE.value: ["text"],
    FrameType.ACK.value: ["event_id"],
    FrameType.PING.value: [],
}


def validate_client_frame(envelope: Envelope) -> None:
    """Validate that *envelope* is a well-formed client frame.

    Raises :class:`ProtocolError` if the type is not a client frame type or
    required payload fields are missing.
    """
    if envelope.type not in CLIENT_FRAME_TYPES:
        raise ProtocolError(
            f"Unknown client frame type: {envelope.type}",
            code="UNKNOWN_FRAME_TYPE",
        )

    required = _CLIENT_REQUIRED_FIELDS.get(envelope.type, [])
    for field in required:
        if field not in envelope.payload:
            raise ProtocolError(
                f"Missing required field '{field}' for frame type '{envelope.type}'",
                code="MISSING_FIELD",
            )


# ---------------------------------------------------------------------------
# Server frame convenience builders
# ---------------------------------------------------------------------------


def frame_hello() -> str:
    """Build a serialized ``hello`` frame (sent to clients on connect)."""
    return serialize(make_envelope(FrameType.HELLO.value))


def frame_auth_success(email: str) -> str:
    """Build a serialized ``auth_success`` frame."""
    return serialize(make_envelope(FrameType.AUTH_SUCCESS.value, email=email))


def frame_auth_error(message: str, code: Optional[str] = None) -> str:
    """Build a serialized ``auth_error`` frame."""
    payload: Dict[str, Any] = {"message": message}
    if code is not None:
        payload["code"] = code
    return serialize(make_envelope(FrameType.AUTH_ERROR.value, **payload))


def frame_snapshot(
    status: str,
    recent_messages: Optional[List[Dict[str, Any]]] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    last_event_id: Optional[str] = None,
) -> str:
    """Build a serialized ``snapshot`` frame."""
    payload: Dict[str, Any] = {"status": status}
    if recent_messages is not None:
        payload["recent_messages"] = recent_messages
    if tasks is not None:
        payload["tasks"] = tasks
    if last_event_id is not None:
        payload["last_event_id"] = last_event_id
    return serialize(make_envelope(FrameType.SNAPSHOT.value, **payload))


def frame_message(
    text: str,
    role: str,
    source: Optional[str] = None,
    chat_id: Optional[str] = None,
    msg_id: Optional[str] = None,
) -> str:
    """Build a serialized ``message`` frame."""
    payload: Dict[str, Any] = {"text": text, "role": role}
    if source is not None:
        payload["source"] = source
    if chat_id is not None:
        payload["chat_id"] = chat_id
    if msg_id is not None:
        payload["msg_id"] = msg_id
    return serialize(make_envelope(FrameType.MESSAGE.value, **payload))


def frame_inbox_update(
    action: str,
    message_id: str,
    preview: Optional[str] = None,
) -> str:
    """Build a serialized ``inbox_update`` frame."""
    payload: Dict[str, Any] = {"action": action, "message_id": message_id}
    if preview is not None:
        payload["preview"] = preview
    return serialize(make_envelope(FrameType.INBOX_UPDATE.value, **payload))


def frame_status(status: str, detail: Optional[str] = None) -> str:
    """Build a serialized ``status`` frame."""
    payload: Dict[str, Any] = {"status": status}
    if detail is not None:
        payload["detail"] = detail
    return serialize(make_envelope(FrameType.STATUS.value, **payload))


def frame_tool_call(
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a serialized ``tool_call`` frame."""
    payload: Dict[str, Any] = {"tool_name": tool_name}
    if arguments is not None:
        payload["arguments"] = arguments
    return serialize(make_envelope(FrameType.TOOL_CALL.value, **payload))


def frame_tool_result(
    tool_name: str,
    result: Optional[Any] = None,
    error: Optional[str] = None,
) -> str:
    """Build a serialized ``tool_result`` frame."""
    payload: Dict[str, Any] = {"tool_name": tool_name}
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    return serialize(make_envelope(FrameType.TOOL_RESULT.value, **payload))


def frame_stream_start(stream_id: Optional[str] = None) -> str:
    """Build a serialized ``stream_start`` frame."""
    payload: Dict[str, Any] = {}
    if stream_id is not None:
        payload["stream_id"] = stream_id
    return serialize(make_envelope(FrameType.STREAM_START.value, **payload))


def frame_stream_delta(text: str, stream_id: Optional[str] = None) -> str:
    """Build a serialized ``stream_delta`` frame."""
    payload: Dict[str, Any] = {"text": text}
    if stream_id is not None:
        payload["stream_id"] = stream_id
    return serialize(make_envelope(FrameType.STREAM_DELTA.value, **payload))


def frame_stream_end(stream_id: Optional[str] = None) -> str:
    """Build a serialized ``stream_end`` frame."""
    payload: Dict[str, Any] = {}
    if stream_id is not None:
        payload["stream_id"] = stream_id
    return serialize(make_envelope(FrameType.STREAM_END.value, **payload))


def frame_agent_started(task: Optional[str] = None) -> str:
    """Build a serialized ``agent_started`` frame."""
    payload: Dict[str, Any] = {}
    if task is not None:
        payload["task"] = task
    return serialize(make_envelope(FrameType.AGENT_STARTED.value, **payload))


def frame_agent_completed(
    task: Optional[str] = None,
    result: Optional[Any] = None,
) -> str:
    """Build a serialized ``agent_completed`` frame."""
    payload: Dict[str, Any] = {}
    if task is not None:
        payload["task"] = task
    if result is not None:
        payload["result"] = result
    return serialize(make_envelope(FrameType.AGENT_COMPLETED.value, **payload))


def frame_error(message: str, code: Optional[str] = None) -> str:
    """Build a serialized ``error`` frame."""
    payload: Dict[str, Any] = {"message": message}
    if code is not None:
        payload["code"] = code
    return serialize(make_envelope(FrameType.ERROR.value, **payload))


def frame_pong() -> str:
    """Build a serialized ``pong`` frame."""
    return serialize(make_envelope(FrameType.PONG.value))
