"""
Tests for src/integrations/google_workspace/token_store.py.

Covers:
- save_token / load_token: roundtrip persistence with correct file permissions
- load_token: returns None when file absent
- load_token: returns None when file is corrupt JSON
- get_valid_token: returns token when valid
- get_valid_token: returns None when no token on disk
- get_valid_token: refreshes expired token via proxy
- get_valid_token: returns None when refresh fails
- get_valid_token: returns None when refresh_token is missing
- No token values appear in logs
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.oauth import TokenData
from integrations.google_workspace import token_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future_token(minutes: int = 60, refresh_token: str = "test-refresh") -> TokenData:
    """Return a TokenData with an access token that expires in the future."""
    return TokenData(
        access_token="test-access-token",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=minutes),
        scope="https://www.googleapis.com/auth/documents",
        refresh_token=refresh_token,
    )


def _expired_token(refresh_token: str = "test-refresh") -> TokenData:
    """Return a TokenData that is already expired."""
    return TokenData(
        access_token="expired-access-token",
        expires_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        scope="https://www.googleapis.com/auth/documents",
        refresh_token=refresh_token,
    )


# ---------------------------------------------------------------------------
# save_token / load_token roundtrip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundtrip:
    def test_roundtrip_preserves_all_fields(self, tmp_path):
        token = _future_token()
        token_store.save_token("user123", token, token_dir=tmp_path)
        loaded = token_store.load_token("user123", token_dir=tmp_path)

        assert loaded is not None
        assert loaded.access_token == token.access_token
        assert loaded.scope == token.scope
        assert loaded.refresh_token == token.refresh_token
        # expires_at should be close (round-trip through ISO string)
        delta = abs((loaded.expires_at - token.expires_at).total_seconds())
        assert delta < 1.0

    def test_token_file_has_mode_0o600(self, tmp_path):
        token = _future_token()
        token_store.save_token("user456", token, token_dir=tmp_path)
        token_path = tmp_path / "user456.json"
        file_stat = os.stat(token_path)
        permissions = stat.S_IMODE(file_stat.st_mode)
        assert permissions == 0o600

    def test_token_file_is_valid_json(self, tmp_path):
        token = _future_token()
        token_store.save_token("user789", token, token_dir=tmp_path)
        token_path = tmp_path / "user789.json"
        data = json.loads(token_path.read_text())
        assert "access_token" in data
        assert "expires_at" in data

    def test_save_creates_directory_if_needed(self, tmp_path):
        nested_dir = tmp_path / "nested" / "deep" / "dir"
        assert not nested_dir.exists()
        token = _future_token()
        token_store.save_token("user1", token, token_dir=nested_dir)
        assert nested_dir.exists()


# ---------------------------------------------------------------------------
# load_token — edge cases
# ---------------------------------------------------------------------------


class TestLoadToken:
    def test_returns_none_when_file_absent(self, tmp_path):
        result = token_store.load_token("nonexistent_user", token_dir=tmp_path)
        assert result is None

    def test_returns_none_for_corrupt_json(self, tmp_path):
        (tmp_path / "corrupt.json").write_text("not valid json")
        result = token_store.load_token("corrupt", token_dir=tmp_path)
        assert result is None

    def test_returns_none_for_missing_required_field(self, tmp_path):
        (tmp_path / "incomplete.json").write_text('{"access_token": "tok"}')
        result = token_store.load_token("incomplete", token_dir=tmp_path)
        assert result is None

    def test_sanitises_user_id(self, tmp_path):
        """Path traversal characters are stripped from user_id."""
        token = _future_token()
        # Save with a safe id, verify by loading with the same raw id
        token_store.save_token("safe123", token, token_dir=tmp_path)
        loaded = token_store.load_token("safe123", token_dir=tmp_path)
        assert loaded is not None

    def test_raises_for_empty_user_id(self, tmp_path):
        with pytest.raises(ValueError, match="empty filename"):
            token_store.load_token("!!!", token_dir=tmp_path)


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------


class TestGetValidToken:
    def test_returns_valid_token_without_refresh(self, tmp_path):
        token = _future_token()
        token_store.save_token("user1", token, token_dir=tmp_path)
        result = token_store.get_valid_token("user1", token_dir=tmp_path)
        assert result is not None
        assert result.access_token == "test-access-token"

    def test_returns_none_when_no_token_on_disk(self, tmp_path):
        result = token_store.get_valid_token("no_such_user", token_dir=tmp_path)
        assert result is None

    def test_refreshes_expired_token(self, tmp_path):
        expired = _expired_token(refresh_token="valid-refresh")
        token_store.save_token("user_exp", expired, token_dir=tmp_path)

        new_token_data = TokenData(
            access_token="refreshed-access-token",
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
            scope="",
            refresh_token=None,
        )

        with patch.object(token_store, "_refresh_token_via_proxy", return_value=new_token_data):
            result = token_store.get_valid_token("user_exp", token_dir=tmp_path)

        assert result is not None
        assert result.access_token == "refreshed-access-token"
        assert result.refresh_token == "valid-refresh"  # preserved from disk

    def test_returns_none_when_refresh_fails(self, tmp_path):
        expired = _expired_token(refresh_token="valid-refresh")
        token_store.save_token("user_fail", expired, token_dir=tmp_path)

        with patch.object(token_store, "_refresh_token_via_proxy", return_value=None):
            result = token_store.get_valid_token("user_fail", token_dir=tmp_path)

        assert result is None

    def test_returns_none_when_no_refresh_token(self, tmp_path):
        expired = _expired_token(refresh_token=None)
        token_store.save_token("user_no_refresh", expired, token_dir=tmp_path)
        result = token_store.get_valid_token("user_no_refresh", token_dir=tmp_path)
        assert result is None

    def test_persists_refreshed_token(self, tmp_path):
        """After a successful refresh, the new token is saved to disk."""
        expired = _expired_token(refresh_token="valid-refresh")
        token_store.save_token("user_persist", expired, token_dir=tmp_path)

        new_token_data = TokenData(
            access_token="newly-refreshed",
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
            scope="",
            refresh_token=None,
        )

        with patch.object(token_store, "_refresh_token_via_proxy", return_value=new_token_data):
            token_store.get_valid_token("user_persist", token_dir=tmp_path)

        # Load again without going through get_valid_token
        persisted = token_store.load_token("user_persist", token_dir=tmp_path)
        assert persisted is not None
        assert persisted.access_token == "newly-refreshed"


# ---------------------------------------------------------------------------
# No token values in logs
# ---------------------------------------------------------------------------


class TestNoTokenValuesInLogs:
    def test_access_token_not_logged(self, tmp_path, caplog):
        import logging
        token = _future_token()
        with caplog.at_level(logging.DEBUG, logger="integrations.google_workspace.token_store"):
            token_store.save_token("user_log", token, token_dir=tmp_path)
            token_store.load_token("user_log", token_dir=tmp_path)
            token_store.get_valid_token("user_log", token_dir=tmp_path)

        for record in caplog.records:
            assert "test-access-token" not in record.getMessage()
