"""
Tests for multiplayer_telegram_bot.session — GroupSession state model.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from multiplayer_telegram_bot.session import (
    SESSION_TTL_SECONDS,
    GroupSession,
    close_session,
    get_active_session,
    is_closure_signal,
    load_sessions,
    open_session,
    purge_expired_sessions,
    refresh_session,
    save_sessions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future(seconds: int = SESSION_TTL_SECONDS) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# GroupSession data model
# ---------------------------------------------------------------------------

class TestGroupSession:
    def test_round_trip(self):
        session = GroupSession(
            chat_id=-100123,
            invoker_user_id=456,
            expires_at=_future(),
            active=True,
        )
        d = session.to_dict()
        restored = GroupSession.from_dict(d)
        assert restored.chat_id == session.chat_id
        assert restored.invoker_user_id == session.invoker_user_id
        assert restored.active == session.active
        # Timestamps compare equal (within milliseconds)
        assert abs((restored.expires_at - session.expires_at).total_seconds()) < 0.001

    def test_is_expired_false_for_future(self):
        session = GroupSession(
            chat_id=-1,
            invoker_user_id=1,
            expires_at=_future(60),
        )
        assert not session.is_expired()

    def test_is_expired_true_for_past(self):
        session = GroupSession(
            chat_id=-1,
            invoker_user_id=1,
            expires_at=_past(1),
        )
        assert session.is_expired()

    def test_from_dict_ensures_timezone(self):
        # Naive datetime string (no +00:00) should be treated as UTC
        d = {
            "chat_id": -1,
            "invoker_user_id": 1,
            "expires_at": "2099-01-01T00:00:00",
            "active": True,
        }
        session = GroupSession.from_dict(d)
        assert session.expires_at.tzinfo is not None

    def test_from_dict_default_active_true(self):
        d = {
            "chat_id": -1,
            "invoker_user_id": 1,
            "expires_at": _future().isoformat(),
        }
        session = GroupSession.from_dict(d)
        assert session.active is True


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_load_sessions_returns_empty_if_no_file(self, tmp_path):
        path = tmp_path / "sessions.json"
        assert load_sessions(path) == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "sessions.json"
        sessions = {
            -100: GroupSession(-100, 1, _future(), True),
            -200: GroupSession(-200, 2, _future(120), False),
        }
        save_sessions(sessions, path)
        loaded = load_sessions(path)
        assert set(loaded.keys()) == {-100, -200}
        assert loaded[-100].invoker_user_id == 1
        assert loaded[-200].active is False

    def test_save_creates_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "sessions.json"
        save_sessions({}, path)
        assert path.exists()

    def test_load_skips_malformed_entries(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text(json.dumps([{"bad": "data"}, {"chat_id": -1, "invoker_user_id": 1, "expires_at": _future().isoformat()}]))
        loaded = load_sessions(path)
        # Only valid entry loaded
        assert -1 in loaded

    def test_load_returns_empty_on_invalid_json(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text("not json")
        assert load_sessions(path) == {}


# ---------------------------------------------------------------------------
# Session state functions
# ---------------------------------------------------------------------------

class TestGetActiveSession:
    def test_returns_none_if_no_file(self, tmp_path):
        assert get_active_session(-1, tmp_path / "sessions.json") is None

    def test_returns_none_if_expired(self, tmp_path):
        path = tmp_path / "sessions.json"
        sessions = {-1: GroupSession(-1, 1, _past(), True)}
        save_sessions(sessions, path)
        assert get_active_session(-1, path) is None

    def test_returns_none_if_inactive(self, tmp_path):
        path = tmp_path / "sessions.json"
        sessions = {-1: GroupSession(-1, 1, _future(), active=False)}
        save_sessions(sessions, path)
        assert get_active_session(-1, path) is None

    def test_returns_session_if_active_and_not_expired(self, tmp_path):
        path = tmp_path / "sessions.json"
        sessions = {-1: GroupSession(-1, 42, _future(), True)}
        save_sessions(sessions, path)
        result = get_active_session(-1, path)
        assert result is not None
        assert result.invoker_user_id == 42


class TestOpenSession:
    def test_creates_new_session(self, tmp_path):
        path = tmp_path / "sessions.json"
        session = open_session(-100, 99, path)
        assert session.chat_id == -100
        assert session.invoker_user_id == 99
        assert session.active is True
        assert not session.is_expired()

    def test_creates_file_on_first_call(self, tmp_path):
        path = tmp_path / "sessions.json"
        open_session(-100, 1, path)
        assert path.exists()

    def test_idempotent_refreshes_ttl(self, tmp_path):
        path = tmp_path / "sessions.json"
        s1 = open_session(-100, 1, path)
        # Move expiry back artificially
        sessions = load_sessions(path)
        sessions[-100].expires_at = _past(60)
        save_sessions(sessions, path)
        # Re-open should refresh
        s2 = open_session(-100, 1, path)
        assert not s2.is_expired()

    def test_updates_invoker(self, tmp_path):
        path = tmp_path / "sessions.json"
        open_session(-100, 1, path)
        s = open_session(-100, 2, path)
        assert s.invoker_user_id == 2


class TestCloseSession:
    def test_marks_inactive(self, tmp_path):
        path = tmp_path / "sessions.json"
        open_session(-100, 1, path)
        close_session(-100, path)
        loaded = load_sessions(path)
        assert loaded[-100].active is False

    def test_noop_if_no_session(self, tmp_path):
        path = tmp_path / "sessions.json"
        # Should not raise
        close_session(-999, path)


class TestRefreshSession:
    def test_extends_expiry(self, tmp_path):
        path = tmp_path / "sessions.json"
        # Create session with short TTL
        sessions = {-1: GroupSession(-1, 1, _future(30), True)}
        save_sessions(sessions, path)
        refreshed = refresh_session(-1, path)
        assert refreshed is not None
        # Should now be ~SESSION_TTL_SECONDS in the future
        delta = (refreshed.expires_at - datetime.now(timezone.utc)).total_seconds()
        assert delta > SESSION_TTL_SECONDS - 5  # within 5s tolerance

    def test_returns_none_if_no_session(self, tmp_path):
        path = tmp_path / "sessions.json"
        assert refresh_session(-999, path) is None

    def test_returns_none_if_inactive(self, tmp_path):
        path = tmp_path / "sessions.json"
        sessions = {-1: GroupSession(-1, 1, _future(), active=False)}
        save_sessions(sessions, path)
        assert refresh_session(-1, path) is None

    def test_refreshes_even_if_technically_expired(self, tmp_path):
        """refresh_session should still extend TTL for a recently-expired session
        (bot is replying, so the session should stay alive)."""
        path = tmp_path / "sessions.json"
        sessions = {-1: GroupSession(-1, 1, _past(5), True)}
        save_sessions(sessions, path)
        refreshed = refresh_session(-1, path)
        assert refreshed is not None
        assert not refreshed.is_expired()


class TestPurgeExpiredSessions:
    def test_removes_expired(self):
        sessions = {
            -1: GroupSession(-1, 1, _past(), True),
            -2: GroupSession(-2, 2, _future(), True),
        }
        purged = purge_expired_sessions(sessions)
        assert -1 not in purged
        assert -2 in purged

    def test_removes_inactive(self):
        sessions = {
            -1: GroupSession(-1, 1, _future(), active=False),
        }
        purged = purge_expired_sessions(sessions)
        assert -1 not in purged

    def test_pure_does_not_modify_input(self):
        original = {-1: GroupSession(-1, 1, _future(), True)}
        purge_expired_sessions(original)
        assert -1 in original  # input unchanged


# ---------------------------------------------------------------------------
# Closure signal detection
# ---------------------------------------------------------------------------

class TestIsClosureSignal:
    @pytest.mark.parametrize("text", [
        "thanks",
        "THANKS",
        "Thank You",
        "thx",
        "got it",
        "gotcha",
        "👍",
        "perfect",
        "done",
        "all set",
        "that's all",
        "never mind",
        "nevermind",
        "ok thanks",
        "ok thank you",
        "cheers",
    ])
    def test_closure_signals(self, text):
        assert is_closure_signal(text), f"Expected closure signal for: {text!r}"

    @pytest.mark.parametrize("text", [
        "hello",
        "can you help me?",
        "thanks for everything but I have another question",
        "not done yet",
        "thankful",
    ])
    def test_non_closure_signals(self, text):
        assert not is_closure_signal(text), f"Should not be closure signal: {text!r}"

    def test_none_returns_false(self):
        assert not is_closure_signal(None)

    def test_empty_string_returns_false(self):
        assert not is_closure_signal("")

    def test_strips_whitespace(self):
        assert is_closure_signal("  thanks  ")
