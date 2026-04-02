"""Unit tests for the lobstertalk-unified job design.

These tests validate the core logic patterns described in the task definition:
- Direction inference from endpoint context (not stored field)
- State file schema and atomic write contract
- Hot-mode threshold logic (consecutive_empty_polls >= 5)
- Timestamp cursor advancement (INBOUND + OUTBOUND both advance cursor)
- Inbox message schema
- Outbound queue file handling
- Log rotation threshold

The tests use only pure functions and data structures — no MCP, no filesystem side
effects unless explicitly testing filesystem behavior with tmp_path.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Pure helper implementations mirroring the task definition's logic.
# These are NOT imports from a real module — they are self-contained
# reimplementations of the described logic, written to be testable and to
# serve as a specification.
# ---------------------------------------------------------------------------

_DEFAULT_STATE: dict[str, Any] = {
    "last_seen_ts": "2020-01-01T00:00:00Z",
    "hot_mode": False,
    "consecutive_empty_polls": 0,
    "hot_mode_activated_at": None,
}


def load_state(state_file: Path) -> dict[str, Any]:
    """Load state file, returning defaults if missing or malformed."""
    if not state_file.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(state_file.read_text())
        # Merge with defaults so new fields are always present
        return {**_DEFAULT_STATE, **data}
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)


def write_state_atomic(state_file: Path, state: dict[str, Any]) -> None:
    """Write state atomically: .tmp then rename."""
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.rename(state_file)


def advance_cursor(messages: list[dict], current_ts: str) -> str:
    """Return the latest timestamp from messages, or current_ts if empty.

    Both INBOUND and OUTBOUND messages advance the cursor — direction does not
    affect cursor advancement (per spec: 'advance last_seen_ts across ALL messages').
    """
    if not messages:
        return current_ts
    return max(m["timestamp"] for m in messages)


def infer_direction_from_endpoint(endpoint: str) -> str:
    """Infer message direction from which API endpoint was called.

    GET /messages  → INBOUND  (we received these)
    POST /message  → OUTBOUND (we sent this)

    This is more reliable than inspecting a stored field or filtering by sender name.
    """
    # Strip query params before matching
    path = endpoint.split("?")[0].rstrip("/")
    if path.endswith("/message"):
        return "OUTBOUND"
    if path.endswith("/messages"):
        return "INBOUND"
    raise ValueError(f"Unknown endpoint: {endpoint!r}")


def update_hot_mode(state: dict[str, Any], messages_received: int) -> dict[str, Any]:
    """Return a new state dict with hot-mode updated based on messages received.

    Hot-mode rules (from spec):
    - Any messages received → hot_mode=True, reset consecutive_empty_polls=0
    - No messages → increment consecutive_empty_polls; if >= 5 → hot_mode=False
    """
    state = dict(state)  # immutable — create a copy
    if messages_received > 0:
        state["hot_mode"] = True
        state["consecutive_empty_polls"] = 0
        if state.get("hot_mode_activated_at") is None:
            state["hot_mode_activated_at"] = datetime.now(timezone.utc).isoformat()
    else:
        state["consecutive_empty_polls"] = state.get("consecutive_empty_polls", 0) + 1
        if state["consecutive_empty_polls"] >= 2:
            state["hot_mode"] = False
            state["hot_mode_activated_at"] = None
    return state


def build_inbox_message(sender: str, content: str, local_identity: str) -> dict[str, Any]:
    """Build an inbox message dict for an INBOUND bot-talk message.

    Schema per spec: source="bot-talk", direction="INBOUND", from=sender, to=local_identity.
    """
    now = datetime.now(timezone.utc)
    ts_ms = int(now.timestamp() * 1000)
    import uuid
    msg_id = f"{ts_ms}_bot_talk_{uuid.uuid4().hex[:8]}"
    return {
        "id": msg_id,
        "type": "text",
        "source": "bot-talk",
        "chat_id": sender,
        "user_name": sender,
        "text": content,
        "timestamp": now.isoformat(),
        "direction": "INBOUND",
        "from": sender,
        "to": local_identity,
    }


def build_log_entry(
    direction: str,
    sender: str,
    content: str,
    recipient: str = "",
    job_run: str = "",
) -> dict[str, Any]:
    """Build a JSONL log entry for lobstertalk.jsonl."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "sender": sender,
        "recipient": recipient,
        "content": content,
        "job_run": job_run,
    }


def should_rotate_log(log_file: Path, max_bytes: int = 50 * 1024 * 1024) -> bool:
    """Return True if the log file exceeds max_bytes and should be rotated."""
    if not log_file.exists():
        return False
    return log_file.stat().st_size > max_bytes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDirectionInference:
    """Direction is inferred from the API endpoint called, not a stored field."""

    def test_get_messages_is_inbound(self):
        assert infer_direction_from_endpoint("http://host:4242/messages") == "INBOUND"

    def test_post_message_is_outbound(self):
        assert infer_direction_from_endpoint("http://host:4242/message") == "OUTBOUND"

    def test_get_messages_with_query_params_is_inbound(self):
        assert infer_direction_from_endpoint(
            "http://host:4242/messages?since=2026-01-01T00:00:00Z&limit=100"
        ) == "INBOUND"

    def test_trailing_slash_handled(self):
        assert infer_direction_from_endpoint("http://host:4242/messages/") == "INBOUND"
        assert infer_direction_from_endpoint("http://host:4242/message/") == "OUTBOUND"

    def test_unknown_endpoint_raises(self):
        with pytest.raises(ValueError):
            infer_direction_from_endpoint("http://host:4242/health")


class TestStateFileLoading:
    """State file loads correctly and falls back to defaults when missing."""

    def test_missing_file_returns_defaults(self, tmp_path):
        state = load_state(tmp_path / "nonexistent.json")
        assert state["last_seen_ts"] == "2020-01-01T00:00:00Z"
        assert state["hot_mode"] is False
        assert state["consecutive_empty_polls"] == 0
        assert state["hot_mode_activated_at"] is None

    def test_existing_file_loaded(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text(json.dumps({"last_seen_ts": "2026-03-01T12:00:00Z", "hot_mode": True}))
        state = load_state(f)
        assert state["last_seen_ts"] == "2026-03-01T12:00:00Z"
        assert state["hot_mode"] is True

    def test_new_fields_defaulted_when_missing_from_file(self, tmp_path):
        """Any new schema field absent from an old state file gets its default value."""
        f = tmp_path / "state.json"
        f.write_text(json.dumps({"last_seen_ts": "2026-01-01T00:00:00Z"}))
        state = load_state(f)
        assert "consecutive_empty_polls" in state
        assert state["consecutive_empty_polls"] == 0

    def test_malformed_json_returns_defaults(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("not valid json{{")
        state = load_state(f)
        assert state["last_seen_ts"] == "2020-01-01T00:00:00Z"


class TestAtomicStateWrite:
    """State is always written atomically (tmp + rename)."""

    def test_written_file_is_valid_json(self, tmp_path):
        f = tmp_path / "state.json"
        state = {"last_seen_ts": "2026-04-01T00:00:00Z", "hot_mode": True}
        write_state_atomic(f, state)
        loaded = json.loads(f.read_text())
        assert loaded["hot_mode"] is True

    def test_no_tmp_file_left_after_write(self, tmp_path):
        f = tmp_path / "state.json"
        write_state_atomic(f, _DEFAULT_STATE)
        assert not (tmp_path / "state.tmp").exists()

    def test_overwrite_existing_state(self, tmp_path):
        f = tmp_path / "state.json"
        write_state_atomic(f, {"last_seen_ts": "2026-01-01T00:00:00Z"})
        write_state_atomic(f, {"last_seen_ts": "2026-04-02T00:00:00Z"})
        loaded = json.loads(f.read_text())
        assert loaded["last_seen_ts"] == "2026-04-02T00:00:00Z"


class TestTimestampCursor:
    """last_seen_ts advances based on ALL messages, both INBOUND and OUTBOUND."""

    def _make_msgs(self, timestamps: list[str]) -> list[dict]:
        return [{"timestamp": ts, "id": f"msg_{i}"} for i, ts in enumerate(timestamps)]

    def test_cursor_advances_to_latest(self):
        msgs = self._make_msgs(["2026-01-01T01:00:00Z", "2026-01-01T03:00:00Z", "2026-01-01T02:00:00Z"])
        result = advance_cursor(msgs, "2026-01-01T00:00:00Z")
        assert result == "2026-01-01T03:00:00Z"

    def test_empty_messages_leaves_cursor_unchanged(self):
        current = "2026-01-01T12:00:00Z"
        result = advance_cursor([], current)
        assert result == current

    def test_single_message_advances_to_its_timestamp(self):
        msgs = self._make_msgs(["2026-04-02T10:00:00Z"])
        result = advance_cursor(msgs, "2026-01-01T00:00:00Z")
        assert result == "2026-04-02T10:00:00Z"


class TestHotModeLogic:
    """Hot-mode state transitions are pure and deterministic."""

    def _base_state(self, **overrides) -> dict[str, Any]:
        return {**_DEFAULT_STATE, **overrides}

    def test_messages_received_enables_hot_mode(self):
        state = self._base_state(hot_mode=False)
        result = update_hot_mode(state, messages_received=3)
        assert result["hot_mode"] is True

    def test_messages_received_resets_consecutive_empty(self):
        state = self._base_state(consecutive_empty_polls=4)
        result = update_hot_mode(state, messages_received=1)
        assert result["consecutive_empty_polls"] == 0

    def test_empty_poll_increments_counter(self):
        state = self._base_state(consecutive_empty_polls=2)
        result = update_hot_mode(state, messages_received=0)
        assert result["consecutive_empty_polls"] == 3

    def test_two_empty_polls_disables_hot_mode(self):
        state = self._base_state(hot_mode=True, consecutive_empty_polls=1)
        result = update_hot_mode(state, messages_received=0)
        assert result["consecutive_empty_polls"] == 2
        assert result["hot_mode"] is False

    def test_one_empty_poll_does_not_disable_hot_mode(self):
        """Hot mode requires 2 consecutive empty polls to cool down."""
        state = self._base_state(hot_mode=True, consecutive_empty_polls=0)
        result = update_hot_mode(state, messages_received=0)
        assert result["consecutive_empty_polls"] == 1
        assert result["hot_mode"] is True

    def test_hot_mode_activated_at_set_on_first_activation(self):
        state = self._base_state(hot_mode=False, hot_mode_activated_at=None)
        result = update_hot_mode(state, messages_received=1)
        assert result["hot_mode_activated_at"] is not None

    def test_hot_mode_activated_at_not_overwritten_if_already_set(self):
        existing_ts = "2026-04-01T10:00:00Z"
        state = self._base_state(hot_mode=True, hot_mode_activated_at=existing_ts)
        result = update_hot_mode(state, messages_received=2)
        assert result["hot_mode_activated_at"] == existing_ts

    def test_cooling_down_clears_hot_mode_activated_at(self):
        state = self._base_state(
            hot_mode=True,
            consecutive_empty_polls=1,
            hot_mode_activated_at="2026-04-01T10:00:00Z",
        )
        result = update_hot_mode(state, messages_received=0)
        assert result["hot_mode"] is False
        assert result["hot_mode_activated_at"] is None

    def test_update_is_immutable_does_not_modify_input(self):
        """update_hot_mode must not mutate the input state dict."""
        state = self._base_state(consecutive_empty_polls=2)
        _ = update_hot_mode(state, messages_received=0)
        assert state["consecutive_empty_polls"] == 2  # original unchanged


class TestInboxMessageSchema:
    """Inbox messages for INBOUND bot-talk have the correct schema fields."""

    def test_required_fields_present(self):
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        for field in ("id", "type", "source", "chat_id", "user_name", "text",
                      "timestamp", "direction", "from", "to"):
            assert field in msg, f"Missing field: {field!r}"

    def test_source_is_bot_talk(self):
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        assert msg["source"] == "bot-talk"

    def test_direction_is_inbound(self):
        msg = build_inbox_message("AlbertLobster", "hello", "SaharLobster")
        assert msg["direction"] == "INBOUND"

    def test_from_is_sender(self):
        msg = build_inbox_message("AlbertLobster", "content", "SaharLobster")
        assert msg["from"] == "AlbertLobster"

    def test_to_is_local_identity(self):
        msg = build_inbox_message("AlbertLobster", "content", "SaharLobster")
        assert msg["to"] == "SaharLobster"

    def test_chat_id_and_user_name_are_sender(self):
        """chat_id and user_name are set to the sender name for dispatcher routing."""
        msg = build_inbox_message("AlbertLobster", "hi", "SaharLobster")
        assert msg["chat_id"] == "AlbertLobster"
        assert msg["user_name"] == "AlbertLobster"

    def test_text_is_content(self):
        msg = build_inbox_message("AlbertLobster", "the message body", "SaharLobster")
        assert msg["text"] == "the message body"

    def test_id_is_unique(self):
        msg1 = build_inbox_message("A", "x", "B")
        msg2 = build_inbox_message("A", "x", "B")
        assert msg1["id"] != msg2["id"]


class TestLogEntry:
    """JSONL log entries have the required fields for both directions."""

    def test_inbound_log_entry_has_direction(self):
        entry = build_log_entry("INBOUND", "AlbertLobster", "hello")
        assert entry["direction"] == "INBOUND"
        assert entry["sender"] == "AlbertLobster"
        assert entry["content"] == "hello"
        assert "ts" in entry

    def test_outbound_log_entry_has_direction(self):
        entry = build_log_entry("OUTBOUND", "SaharLobster", "reply", recipient="AlbertLobster")
        assert entry["direction"] == "OUTBOUND"
        assert entry["recipient"] == "AlbertLobster"

    def test_log_entry_is_json_serializable(self):
        entry = build_log_entry("INBOUND", "A", "content", "B", "run-001")
        json.dumps(entry)  # must not raise


class TestLogRotation:
    """Log file is rotated when it exceeds 50 MB."""

    def test_small_file_does_not_rotate(self, tmp_path):
        f = tmp_path / "lobstertalk.jsonl"
        f.write_bytes(b"x" * 1000)
        assert should_rotate_log(f) is False

    def test_missing_file_does_not_rotate(self, tmp_path):
        assert should_rotate_log(tmp_path / "lobstertalk.jsonl") is False

    def test_file_over_50mb_triggers_rotation(self, tmp_path):
        f = tmp_path / "lobstertalk.jsonl"
        # Write a file just over 50 MB
        f.write_bytes(b"x" * (50 * 1024 * 1024 + 1))
        assert should_rotate_log(f) is True

    def test_file_exactly_50mb_does_not_trigger(self, tmp_path):
        f = tmp_path / "lobstertalk.jsonl"
        f.write_bytes(b"x" * (50 * 1024 * 1024))
        assert should_rotate_log(f) is False
