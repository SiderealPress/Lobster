"""
Unit tests for bot_talk.schema — Signal Theory structured message envelope.

Tests cover:
- SpeechAct and Genre enums (all values present)
- BotTalkMessage construction (direct and convenience constructors)
- to_dict() serialisation (including legacy compat fields)
- from_dict() deserialisation (structured and legacy messages)
- ACK mechanism
- Helper predicates (needs_ack, is_routing_to_human, requires_response)
- _infer_speech_act fallback for legacy messages
"""

import sys
from pathlib import Path

import pytest

# Ensure tooling/src is on path
_SRC = Path(__file__).parent.parent.parent.parent / "tooling" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bot_talk.schema import BotTalkMessage, Genre, SpeechAct, _infer_speech_act


# ---------------------------------------------------------------------------
# Enum completeness
# ---------------------------------------------------------------------------

class TestSpeechActEnum:
    def test_all_seven_values_exist(self):
        values = {sa.value for sa in SpeechAct}
        assert values == {"heartbeat", "inform", "query", "commit", "decide", "alert", "ack"}

    def test_string_coercion(self):
        assert SpeechAct("heartbeat") is SpeechAct.HEARTBEAT
        assert SpeechAct("alert") is SpeechAct.ALERT

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            SpeechAct("unknown_value")


class TestGenreEnum:
    def test_all_values_exist(self):
        values = {g.value for g in Genre}
        assert "heartbeat" in values
        assert "task-update" in values
        assert "query" in values
        assert "alert" in values
        assert "decision" in values
        assert "status-update" in values

    def test_string_coercion(self):
        assert Genre("heartbeat") is Genre.HEARTBEAT
        assert Genre("alert") is Genre.ALERT


# ---------------------------------------------------------------------------
# Direct construction
# ---------------------------------------------------------------------------

class TestBotTalkMessageConstruction:
    def test_required_fields_stored(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body="all systems nominal",
        )
        assert msg.sender == "OperatorLobster"
        assert msg.genre is Genre.HEARTBEAT
        assert msg.speech_act is SpeechAct.HEARTBEAT
        assert msg.body == {"text": "all systems nominal"}

    def test_string_body_wrapped_in_dict(self):
        msg = BotTalkMessage(
            sender="AlbertLobster",
            genre=Genre.STATUS_UPDATE,
            speech_act=SpeechAct.INFORM,
            body="simple text",
        )
        assert msg.body == {"text": "simple text"}

    def test_dict_body_passed_through(self):
        body = {"text": "hello", "extra": 42}
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.QUERY,
            speech_act=SpeechAct.QUERY,
            body=body,
        )
        assert msg.body == body

    def test_ts_auto_generated_when_absent(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body="ping",
        )
        assert msg.ts  # non-empty
        assert "T" in msg.ts  # ISO-8601 contains T

    def test_explicit_ts_accepted(self):
        ts = "2026-03-26T02:00:00+00:00"
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body="ping",
            ts=ts,
        )
        assert msg.ts == ts

    def test_string_genre_coerced_to_enum(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre="heartbeat",
            speech_act="heartbeat",
            body="ping",
        )
        assert msg.genre is Genre.HEARTBEAT
        assert msg.speech_act is SpeechAct.HEARTBEAT

    def test_default_ack_required_is_false(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body="ping",
        )
        assert msg.ack_required is False

    def test_message_id_auto_generated(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body="ping",
        )
        assert msg.message_id
        assert "OperatorLobster" in msg.message_id

    def test_explicit_message_id_accepted(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body="ping",
            message_id="custom-id-123",
        )
        assert msg.message_id == "custom-id-123"


# ---------------------------------------------------------------------------
# to_dict() serialisation
# ---------------------------------------------------------------------------

class TestToDictSerialisation:
    def _make_msg(self, **kwargs) -> BotTalkMessage:
        defaults = dict(
            sender="OperatorLobster",
            genre=Genre.HEARTBEAT,
            speech_act=SpeechAct.HEARTBEAT,
            body="all systems nominal",
        )
        defaults.update(kwargs)
        return BotTalkMessage(**defaults)

    def test_includes_legacy_fields(self):
        d = self._make_msg().to_dict()
        assert "sender" in d
        assert "tier" in d
        assert "genre" in d
        assert "content" in d

    def test_includes_structured_fields(self):
        d = self._make_msg().to_dict()
        assert "speech_act" in d
        assert "ts" in d
        assert "message_id" in d
        assert "ack_required" in d
        assert "body" in d

    def test_speech_act_is_string_value(self):
        d = self._make_msg().to_dict()
        assert d["speech_act"] == "heartbeat"

    def test_genre_is_string_value(self):
        d = self._make_msg().to_dict()
        assert d["genre"] == "heartbeat"

    def test_legacy_content_contains_speech_act_label(self):
        d = self._make_msg().to_dict()
        assert "HEARTBEAT" in d["content"]

    def test_reply_to_absent_when_none(self):
        d = self._make_msg().to_dict()
        assert "reply_to" not in d

    def test_reply_to_present_when_set(self):
        d = self._make_msg(reply_to="2026-01-01T00:00:00Z").to_dict()
        assert d["reply_to"] == "2026-01-01T00:00:00Z"

    def test_ack_required_serialised_as_bool(self):
        d = self._make_msg(ack_required=True).to_dict()
        assert d["ack_required"] is True


# ---------------------------------------------------------------------------
# from_dict() deserialisation
# ---------------------------------------------------------------------------

class TestFromDictDeserialisation:
    def test_roundtrip_structured_message(self):
        orig = BotTalkMessage(
            sender="AlbertLobster",
            genre=Genre.QUERY,
            speech_act=SpeechAct.QUERY,
            body={"text": "What is the status of issue #22?"},
            ack_required=True,
            message_id="albert:2026-03-26T02:00:00Z",
        )
        restored = BotTalkMessage.from_dict(orig.to_dict())
        assert restored.sender == orig.sender
        assert restored.speech_act == orig.speech_act
        assert restored.genre == orig.genre
        assert restored.ack_required == orig.ack_required
        assert restored.message_id == orig.message_id

    def test_legacy_message_without_speech_act(self):
        """Legacy freeform messages (pre-schema) parse without raising."""
        raw = {
            "sender": "AlbertLobster",
            "tier": "TIER-BOT",
            "genre": "heartbeat",
            "content": "All systems nominal",
        }
        msg = BotTalkMessage.from_dict(raw)
        assert msg.sender == "AlbertLobster"
        assert msg.speech_act == SpeechAct.HEARTBEAT  # inferred from genre
        assert msg.ack_required is False

    def test_unknown_genre_falls_back_to_status_update(self):
        raw = {
            "sender": "AlbertLobster",
            "genre": "SomeFutureGenre",
            "content": "hello",
        }
        msg = BotTalkMessage.from_dict(raw)
        assert msg.genre is Genre.STATUS_UPDATE

    def test_unknown_speech_act_falls_back_to_inform(self):
        raw = {
            "sender": "AlbertLobster",
            "genre": "Heartbeat",
            "speech_act": "future_act",
            "content": "hello",
        }
        msg = BotTalkMessage.from_dict(raw)
        assert msg.speech_act is SpeechAct.INFORM

    def test_reply_to_preserved(self):
        raw = {
            "sender": "OperatorLobster",
            "genre": "status-update",
            "speech_act": "inform",
            "content": "ack",
            "reply_to": "2026-03-26T01:00:00Z",
        }
        msg = BotTalkMessage.from_dict(raw)
        assert msg.reply_to == "2026-03-26T01:00:00Z"


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

class TestConvenienceConstructors:
    def test_heartbeat_constructor(self):
        msg = BotTalkMessage.heartbeat(
            sender="OperatorLobster",
            body="all systems nominal",
            next_expected_at="2026-03-26T03:00:00Z",
        )
        assert msg.speech_act is SpeechAct.HEARTBEAT
        assert msg.genre is Genre.HEARTBEAT
        assert msg.ack_required is False
        assert msg.body["next_expected_at"] == "2026-03-26T03:00:00Z"
        assert msg.body["text"] == "all systems nominal"

    def test_heartbeat_without_next_expected_at(self):
        msg = BotTalkMessage.heartbeat(sender="OperatorLobster", body="ping")
        assert "next_expected_at" not in msg.body

    def test_query_constructor(self):
        msg = BotTalkMessage.query(
            sender="AlbertLobster",
            body="What is the status of issue #22?",
        )
        assert msg.speech_act is SpeechAct.QUERY
        assert msg.genre is Genre.QUERY
        assert msg.ack_required is True

    def test_query_ack_required_overrideable(self):
        msg = BotTalkMessage.query(
            sender="AlbertLobster",
            body="FYI query",
            ack_required=False,
        )
        assert msg.ack_required is False

    def test_inform_constructor(self):
        msg = BotTalkMessage.inform(sender="OperatorLobster", body="Task #14 done")
        assert msg.speech_act is SpeechAct.INFORM
        assert msg.ack_required is False

    def test_inform_custom_genre(self):
        msg = BotTalkMessage.inform(
            sender="OperatorLobster",
            body="Task completed",
            genre=Genre.TASK_UPDATE,
        )
        assert msg.genre is Genre.TASK_UPDATE

    def test_alert_constructor(self):
        msg = BotTalkMessage.alert(
            sender="OperatorLobster",
            body="Bot-talk API is down — 2h outage",
        )
        assert msg.speech_act is SpeechAct.ALERT
        assert msg.genre is Genre.ALERT
        assert msg.ack_required is True

    def test_ack_constructor(self):
        msg = BotTalkMessage.ack(
            sender="OperatorLobster",
            reply_to_id="albert:2026-03-26T02:00:00Z",
            reply_to_ts="2026-03-26T02:00:00Z",
        )
        assert msg.speech_act is SpeechAct.ACK
        assert msg.reply_to == "2026-03-26T02:00:00Z"
        assert msg.body["ack_for"] == "albert:2026-03-26T02:00:00Z"
        assert msg.ack_required is False


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

class TestPredicates:
    def test_needs_ack_true_when_ack_required(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.QUERY,
            speech_act=SpeechAct.QUERY,
            body="question",
            ack_required=True,
        )
        assert msg.needs_ack() is True

    def test_needs_ack_false_for_heartbeat(self):
        msg = BotTalkMessage.heartbeat(sender="OperatorLobster", body="ping")
        assert msg.needs_ack() is False

    def test_is_routing_to_human_for_alert(self):
        msg = BotTalkMessage.alert(sender="AlbertLobster", body="CRITICAL")
        assert msg.is_routing_to_human() is True

    def test_is_routing_to_human_false_for_inform(self):
        msg = BotTalkMessage.inform(sender="OperatorLobster", body="update")
        assert msg.is_routing_to_human() is False

    def test_requires_response_true_for_query(self):
        msg = BotTalkMessage.query(sender="AlbertLobster", body="?")
        assert msg.requires_response() is True

    def test_requires_response_true_for_alert(self):
        msg = BotTalkMessage.alert(sender="OperatorLobster", body="!")
        assert msg.requires_response() is True

    def test_requires_response_false_for_heartbeat(self):
        msg = BotTalkMessage.heartbeat(sender="OperatorLobster", body="ping")
        assert msg.requires_response() is False

    def test_requires_response_true_when_ack_required_on_inform(self):
        msg = BotTalkMessage(
            sender="OperatorLobster",
            genre=Genre.STATUS_UPDATE,
            speech_act=SpeechAct.INFORM,
            body="please confirm receipt",
            ack_required=True,
        )
        assert msg.requires_response() is True


# ---------------------------------------------------------------------------
# _infer_speech_act (legacy compat helper)
# ---------------------------------------------------------------------------

class TestInferSpeechAct:
    def test_heartbeat_genre_maps_to_heartbeat_act(self):
        assert _infer_speech_act(Genre.HEARTBEAT) is SpeechAct.HEARTBEAT

    def test_query_genre_maps_to_query_act(self):
        assert _infer_speech_act(Genre.QUERY) is SpeechAct.QUERY

    def test_decision_genre_maps_to_decide_act(self):
        assert _infer_speech_act(Genre.DECISION) is SpeechAct.DECIDE

    def test_alert_genre_maps_to_alert_act(self):
        assert _infer_speech_act(Genre.ALERT) is SpeechAct.ALERT

    def test_task_update_maps_to_inform(self):
        assert _infer_speech_act(Genre.TASK_UPDATE) is SpeechAct.INFORM

    def test_status_update_maps_to_inform(self):
        assert _infer_speech_act(Genre.STATUS_UPDATE) is SpeechAct.INFORM
