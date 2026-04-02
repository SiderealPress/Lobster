"""Unit tests for lobstertalk-unified direction inference logic.

PR A: direction fix — direction is inferred from which API endpoint is called,
not from a stored field or sender-name heuristic.

Old behavior (fragile): jobs inspected a `direction` field that might be absent,
or filtered by sender name (silently broken if a new Lobster joins the channel).

New behavior: GET /messages → INBOUND, POST /message → OUTBOUND.
No special cases, no stored fields required.

These tests are written spec-first and serve as a living contract for the
`lobstertalk-unified` task definition.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Pure helper implementations mirroring the task definition's direction
# inference logic. Self-contained — not imported from a real module.
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
        return {**_DEFAULT_STATE, **data}
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)


def write_state_atomic(state_file: Path, state: dict[str, Any]) -> None:
    """Write state atomically: .tmp then rename."""
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.rename(state_file)


def infer_direction_from_endpoint(endpoint: str) -> str:
    """Infer message direction from which API endpoint was called.

    GET /messages  → INBOUND  (we received these)
    POST /message  → OUTBOUND (we sent this)

    This is more reliable than inspecting a stored field or filtering by sender name.
    The old jobs used sender-name heuristics that silently break when new Lobster
    instances join the channel.
    """
    path = endpoint.split("?")[0].rstrip("/")
    if path.endswith("/message"):
        return "OUTBOUND"
    if path.endswith("/messages"):
        return "INBOUND"
    raise ValueError(f"Unknown endpoint: {endpoint!r}")


def advance_cursor(messages: list[dict], current_ts: str) -> str:
    """Return the latest timestamp from messages, or current_ts if empty.

    Both INBOUND and OUTBOUND messages advance the cursor.
    """
    if not messages:
        return current_ts
    return max(m["timestamp"] for m in messages)


def update_hot_mode(state: dict[str, Any], messages_received: int) -> dict[str, Any]:
    """Return updated state with hot-mode transitions applied.

    Hot-mode rules:
    - Any messages received → hot_mode=True, reset consecutive_empty_polls=0
    - No messages → increment consecutive_empty_polls; if >= 2 → hot_mode=False
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


def filter_self_messages(messages: list[dict], local_identity: str) -> list[dict]:
    """Filter out messages sent by the local Lobster instance.

    Messages where sender == local_identity are outbound context logs mirrored
    back to the bot-talk server by the email-autoresponder skill (prefixed
    "[INBOUND from TELEGRAM]" or "[OUTBOUND →]"). They are not incoming messages
    from other Lobster instances and must not be re-routed to the inbox.
    """
    return [m for m in messages if m.get("sender") != local_identity]


def should_rotate_log(log_file: Path, max_bytes: int = 50 * 1024 * 1024) -> bool:
    """Return True if the log file exceeds max_bytes and should be rotated."""
    if not log_file.exists():
        return False
    return log_file.stat().st_size > max_bytes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDirectionInference:
    """Direction is inferred from the API endpoint called, not a stored field.

    Old approach (fragile): check message.get('direction') or filter by sender name.
    New approach (reliable): GET /messages → INBOUND, POST /message → OUTBOUND.
    """

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

    def test_no_sender_name_needed(self):
        """Direction inference requires no sender identity — works for any number of Lobsters."""
        # Old jobs: GET /messages?sender=AlbertLobster — breaks when Carol joins
        # New jobs: endpoint alone determines direction
        endpoint = "http://host:4242/messages?since=2026-01-01T00:00:00Z&limit=100"
        # No sender filtering needed; the endpoint call itself implies direction
        assert infer_direction_from_endpoint(endpoint) == "INBOUND"

    def test_no_stored_direction_field_needed(self):
        """Direction inference does not depend on a 'direction' key in the message."""
        # Simulate a message with NO direction field (as would come from the API)
        msg_without_direction = {
            "id": "abc123",
            "sender": "AlbertLobster",
            "content": "hello",
            "timestamp": "2026-04-02T10:00:00Z",
        }
        # Direction is inferred from which endpoint the message was received on
        direction = infer_direction_from_endpoint("http://host:4242/messages")
        assert direction == "INBOUND"
        # No KeyError, no missing field, no special case
        assert "direction" not in msg_without_direction  # message didn't need this field


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
        write_state_atomic(f, {"last_seen_ts": "2026-04-01T00:00:00Z", "hot_mode": True})
        loaded = json.loads(f.read_text())
        assert loaded["hot_mode"] is True

    def test_no_tmp_file_left_after_write(self, tmp_path):
        f = tmp_path / "state.json"
        write_state_atomic(f, _DEFAULT_STATE)
        assert not (tmp_path / "state.tmp").exists()


class TestTimestampCursor:
    """last_seen_ts advances based on ALL messages, both INBOUND and OUTBOUND."""

    def _make_msgs(self, timestamps: list[str]) -> list[dict]:
        return [{"timestamp": ts, "id": f"msg_{i}"} for i, ts in enumerate(timestamps)]

    def test_cursor_advances_to_latest(self):
        msgs = self._make_msgs([
            "2026-01-01T01:00:00Z",
            "2026-01-01T03:00:00Z",
            "2026-01-01T02:00:00Z",
        ])
        result = advance_cursor(msgs, "2026-01-01T00:00:00Z")
        assert result == "2026-01-01T03:00:00Z"

    def test_empty_messages_leaves_cursor_unchanged(self):
        current = "2026-01-01T12:00:00Z"
        result = advance_cursor([], current)
        assert result == current

    def test_cursor_advances_regardless_of_direction_field(self):
        """Both INBOUND and OUTBOUND messages advance the cursor."""
        msgs = [
            {"timestamp": "2026-01-01T02:00:00Z", "id": "1"},  # INBOUND
            {"timestamp": "2026-01-01T04:00:00Z", "id": "2"},  # OUTBOUND
        ]
        result = advance_cursor(msgs, "2026-01-01T00:00:00Z")
        assert result == "2026-01-01T04:00:00Z"


class TestHotModeLogic:
    """Hot-mode state transitions are pure and deterministic."""

    def _base_state(self, **overrides) -> dict[str, Any]:
        return {**_DEFAULT_STATE, **overrides}

    def test_messages_received_enables_hot_mode(self):
        state = self._base_state(hot_mode=False)
        result = update_hot_mode(state, messages_received=3)
        assert result["hot_mode"] is True

    def test_two_empty_polls_disables_hot_mode(self):
        state = self._base_state(hot_mode=True, consecutive_empty_polls=1)
        result = update_hot_mode(state, messages_received=0)
        assert result["consecutive_empty_polls"] == 2
        assert result["hot_mode"] is False

    def test_one_empty_poll_does_not_disable_hot_mode(self):
        state = self._base_state(hot_mode=True, consecutive_empty_polls=0)
        result = update_hot_mode(state, messages_received=0)
        assert result["consecutive_empty_polls"] == 1
        assert result["hot_mode"] is True

    def test_update_is_immutable_does_not_modify_input(self):
        state = self._base_state(consecutive_empty_polls=2)
        _ = update_hot_mode(state, messages_received=0)
        assert state["consecutive_empty_polls"] == 2


class TestSelfMessageFilter:
    """Messages where sender == local identity are context mirrors, not real inbound messages.

    The email-autoresponder skill logs both sides of conversations to bot-talk with
    prefixes like "[INBOUND from TELEGRAM]" or "[OUTBOUND →]". These have
    sender == "SaharLobster" (or whatever the local identity is) and must be
    skipped during the GET /messages receive step.
    """

    LOCAL_IDENTITY = "SaharLobster"

    def _make_msg(self, sender: str, content: str = "hello") -> dict:
        return {
            "id": "msg_test",
            "sender": sender,
            "content": content,
            "timestamp": "2026-04-02T10:00:00Z",
        }

    def test_self_messages_are_filtered_out(self):
        msgs = [
            self._make_msg("SaharLobster", "[INBOUND from TELEGRAM] user: hello"),
            self._make_msg("AlbertLobster", "hi there"),
        ]
        result = filter_self_messages(msgs, self.LOCAL_IDENTITY)
        assert len(result) == 1
        assert result[0]["sender"] == "AlbertLobster"

    def test_all_self_messages_filtered(self):
        msgs = [
            self._make_msg("SaharLobster", "[OUTBOUND →] hi"),
            self._make_msg("SaharLobster", "[INBOUND from TELEGRAM] user: test"),
        ]
        result = filter_self_messages(msgs, self.LOCAL_IDENTITY)
        assert result == []

    def test_non_self_messages_pass_through(self):
        msgs = [
            self._make_msg("AlbertLobster", "hello from albert"),
            self._make_msg("CarolLobster", "hello from carol"),
        ]
        result = filter_self_messages(msgs, self.LOCAL_IDENTITY)
        assert len(result) == 2

    def test_empty_list_returns_empty(self):
        assert filter_self_messages([], self.LOCAL_IDENTITY) == []

    def test_filter_is_identity_specific(self):
        """Filter only removes messages matching the exact local identity."""
        msgs = [
            self._make_msg("SaharLobster2"),  # different identity, should pass through
            self._make_msg("SaharLobster"),   # exact match, should be filtered
        ]
        result = filter_self_messages(msgs, self.LOCAL_IDENTITY)
        assert len(result) == 1
        assert result[0]["sender"] == "SaharLobster2"


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
        f.write_bytes(b"x" * (50 * 1024 * 1024 + 1))
        assert should_rotate_log(f) is True
