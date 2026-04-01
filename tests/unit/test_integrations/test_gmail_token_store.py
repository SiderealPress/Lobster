"""
Unit tests for src/integrations/gmail/token_store.py (BIS-256).

These tests mirror the structure of test_google_calendar_token_store.py,
adapted for the Gmail token store.  All file I/O and HTTP are mocked.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.oauth import TokenData
from integrations.gmail import token_store as ts

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_FUTURE = datetime.now(tz=timezone.utc) + timedelta(hours=2)
_EXPIRED = datetime.now(tz=timezone.utc) - timedelta(hours=1)

_VALID_TOKEN = TokenData(
    access_token="valid-access-token",
    expires_at=_FUTURE,
    scope="https://mail.google.com/",
    refresh_token="refresh-token-abc",
)

_EXPIRED_TOKEN = TokenData(
    access_token="expired-access-token",
    expires_at=_EXPIRED,
    scope="https://mail.google.com/",
    refresh_token="refresh-token-xyz",
)


# ---------------------------------------------------------------------------
# _token_path — pure function
# ---------------------------------------------------------------------------


class TestTokenPath:
    def test_basic_user_id(self, tmp_path):
        path = ts._token_path("12345", tmp_path)
        assert path == tmp_path / "12345.json"

    def test_sanitises_special_chars(self, tmp_path):
        path = ts._token_path("../evil", tmp_path)
        assert "/" not in path.name
        assert ".." not in path.name

    def test_empty_after_sanitise_raises(self, tmp_path):
        with pytest.raises(ValueError):
            ts._token_path("!@#$%", tmp_path)

    def test_hyphen_and_underscore_preserved(self, tmp_path):
        path = ts._token_path("user-123_abc", tmp_path)
        assert path.name == "user-123_abc.json"


# ---------------------------------------------------------------------------
# _token_to_dict / _dict_to_token — pure serialisation round-trip
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_round_trip_preserves_all_fields(self):
        d = ts._token_to_dict(_VALID_TOKEN)
        restored = ts._dict_to_token(d)
        assert restored.access_token == _VALID_TOKEN.access_token
        assert restored.refresh_token == _VALID_TOKEN.refresh_token
        assert restored.scope == _VALID_TOKEN.scope
        # expires_at round-trips through isoformat
        assert restored.expires_at == _VALID_TOKEN.expires_at

    def test_round_trip_with_null_refresh_token(self):
        token = TokenData(
            access_token="tok",
            expires_at=_FUTURE,
            scope="",
            refresh_token=None,
        )
        d = ts._token_to_dict(token)
        restored = ts._dict_to_token(d)
        assert restored.refresh_token is None

    def test_naive_datetime_gets_utc_tzinfo(self):
        data = {
            "access_token": "tok",
            "expires_at": "2026-01-01T12:00:00",  # no tzinfo
            "scope": "",
            "refresh_token": None,
        }
        token = ts._dict_to_token(data)
        assert token.expires_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# _save_token_local / _load_token_local — file I/O
# ---------------------------------------------------------------------------


class TestSaveLoadLocal:
    def test_save_creates_file(self, tmp_path):
        ts._save_token_local("99", _VALID_TOKEN, tmp_path)
        assert (tmp_path / "99.json").exists()

    def test_save_sets_mode_0600(self, tmp_path):
        ts._save_token_local("99", _VALID_TOKEN, tmp_path)
        mode = (tmp_path / "99.json").stat().st_mode
        assert mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR

    def test_load_returns_none_when_file_absent(self, tmp_path):
        result = ts._load_token_local("nonexistent", tmp_path)
        assert result is None

    def test_load_returns_token_after_save(self, tmp_path):
        ts._save_token_local("42", _VALID_TOKEN, tmp_path)
        loaded = ts._load_token_local("42", tmp_path)
        assert loaded is not None
        assert loaded.access_token == _VALID_TOKEN.access_token

    def test_load_returns_none_on_corrupted_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        # Rename to match user_id "bad"
        result = ts._load_token_local("bad", tmp_path)
        assert result is None

    def test_save_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        ts._save_token_local("x", _VALID_TOKEN, deep)
        assert (deep / "x.json").exists()


# ---------------------------------------------------------------------------
# _refresh_token_via_proxy — HTTP side-effecting boundary
# ---------------------------------------------------------------------------


class TestRefreshTokenViaProxy:
    def _mock_success_response(self) -> MagicMock:
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {
            "access_token": "new-access-token",
            "expires_in": 3600,
        }
        return resp

    def test_returns_new_token_on_success(self):
        with patch.dict(
            "os.environ",
            {"LOBSTER_INTERNAL_SECRET": "secret"},
        ), patch(
            "integrations.gmail.token_store.requests.post",
            return_value=self._mock_success_response(),
        ):
            result = ts._refresh_token_via_proxy("refresh-tok")

        assert result is not None
        assert result.access_token == "new-access-token"
        assert result.expires_at > datetime.now(tz=timezone.utc)

    def test_returns_none_when_secret_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = ts._refresh_token_via_proxy("refresh-tok")
        assert result is None

    def test_returns_none_on_network_error(self):
        import requests as req_lib

        with patch.dict("os.environ", {"LOBSTER_INTERNAL_SECRET": "secret"}), patch(
            "integrations.gmail.token_store.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("refused"),
        ):
            result = ts._refresh_token_via_proxy("refresh-tok")
        assert result is None

    def test_returns_none_on_non_ok_response(self):
        bad = MagicMock()
        bad.ok = False
        bad.status_code = 500
        bad.text = "Internal Server Error"
        with patch.dict("os.environ", {"LOBSTER_INTERNAL_SECRET": "secret"}), patch(
            "integrations.gmail.token_store.requests.post",
            return_value=bad,
        ):
            result = ts._refresh_token_via_proxy("refresh-tok")
        assert result is None

    def test_returns_none_on_bad_json(self):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"unexpected": "keys"}
        with patch.dict("os.environ", {"LOBSTER_INTERNAL_SECRET": "secret"}), patch(
            "integrations.gmail.token_store.requests.post",
            return_value=resp,
        ):
            result = ts._refresh_token_via_proxy("refresh-tok")
        assert result is None

    def test_uses_gmail_refresh_endpoint(self):
        """Refresh must call the gmail-specific endpoint, not the calendar one."""
        with patch.dict("os.environ", {"LOBSTER_INTERNAL_SECRET": "secret"}), patch(
            "integrations.gmail.token_store.requests.post",
            return_value=self._mock_success_response(),
        ) as mock_post:
            ts._refresh_token_via_proxy("refresh-tok")

        called_url = mock_post.call_args.args[0]
        assert "gmail" in called_url
        assert "calendar" not in called_url


# ---------------------------------------------------------------------------
# get_valid_token — composition of load + check + refresh
# ---------------------------------------------------------------------------


class TestGetValidToken:
    def test_returns_none_when_no_token(self, tmp_path):
        result = ts.get_valid_token("unknown", token_dir=tmp_path)
        assert result is None

    def test_returns_valid_token_directly(self, tmp_path):
        ts._save_token_local("u1", _VALID_TOKEN, tmp_path)
        result = ts.get_valid_token("u1", token_dir=tmp_path)
        assert result is not None
        assert result.access_token == _VALID_TOKEN.access_token

    def test_refreshes_expired_token(self, tmp_path):
        ts._save_token_local("u2", _EXPIRED_TOKEN, tmp_path)

        refreshed_partial = TokenData(
            access_token="refreshed-access",
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
            scope="",
            refresh_token=None,
        )

        with patch.dict("os.environ", {"LOBSTER_INTERNAL_SECRET": "secret"}), patch(
            "integrations.gmail.token_store._refresh_token_via_proxy",
            return_value=refreshed_partial,
        ):
            result = ts.get_valid_token("u2", token_dir=tmp_path)

        assert result is not None
        assert result.access_token == "refreshed-access"
        # refresh_token preserved from original
        assert result.refresh_token == _EXPIRED_TOKEN.refresh_token

    def test_returns_none_when_expired_no_refresh_token(self, tmp_path):
        expired_no_rt = TokenData(
            access_token="expired",
            expires_at=_EXPIRED,
            scope="",
            refresh_token=None,
        )
        ts._save_token_local("u3", expired_no_rt, tmp_path)
        result = ts.get_valid_token("u3", token_dir=tmp_path)
        assert result is None

    def test_returns_none_when_refresh_fails(self, tmp_path):
        ts._save_token_local("u4", _EXPIRED_TOKEN, tmp_path)
        with patch.dict("os.environ", {"LOBSTER_INTERNAL_SECRET": "secret"}), patch(
            "integrations.gmail.token_store._refresh_token_via_proxy",
            return_value=None,
        ):
            result = ts.get_valid_token("u4", token_dir=tmp_path)
        assert result is None

    def test_saves_refreshed_token_to_disk(self, tmp_path):
        ts._save_token_local("u5", _EXPIRED_TOKEN, tmp_path)

        refreshed_partial = TokenData(
            access_token="refreshed-persist",
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
            scope="",
            refresh_token=None,
        )

        with patch.dict("os.environ", {"LOBSTER_INTERNAL_SECRET": "secret"}), patch(
            "integrations.gmail.token_store._refresh_token_via_proxy",
            return_value=refreshed_partial,
        ):
            ts.get_valid_token("u5", token_dir=tmp_path)

        # Read back from disk to confirm persistence
        persisted = ts._load_token_local("u5", tmp_path)
        assert persisted is not None
        assert persisted.access_token == "refreshed-persist"


# ---------------------------------------------------------------------------
# Token isolation: gmail-tokens != gcal-tokens
# ---------------------------------------------------------------------------


class TestTokenIsolation:
    def test_gmail_token_dir_is_separate_from_gcal(self):
        """The default token directory must be gmail-tokens, not gcal-tokens."""
        assert "gmail" in str(ts._TOKEN_DIR)
        assert "gcal" not in str(ts._TOKEN_DIR)

    def test_saving_gmail_token_does_not_touch_gcal_dir(self, tmp_path):
        gmail_dir = tmp_path / "gmail-tokens"
        gcal_dir = tmp_path / "gcal-tokens"
        gcal_dir.mkdir()

        ts._save_token_local("99", _VALID_TOKEN, gmail_dir)

        # gcal directory should still be empty
        assert list(gcal_dir.iterdir()) == []

    def test_loading_from_gmail_dir_does_not_read_gcal_dir(self, tmp_path):
        gmail_dir = tmp_path / "gmail-tokens"
        gcal_dir = tmp_path / "gcal-tokens"
        gcal_dir.mkdir()

        # Put a token in gcal dir only
        ts._save_token_local("99", _VALID_TOKEN, gcal_dir)

        # Loading from gmail dir must return None (not the gcal token)
        result = ts._load_token_local("99", gmail_dir)
        assert result is None
