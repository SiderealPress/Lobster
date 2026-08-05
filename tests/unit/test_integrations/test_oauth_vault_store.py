"""
Tests for src/integrations/oauth_vault/vault_store.py (BIS-732 / Slice 4).

This is the generic storage layer extracted from
google_calendar/token_store.py, gmail/token_store.py, and
google_workspace/token_store.py's identical save/load/serialize logic. These
tests mirror the corresponding characterization assertions in
test_google_calendar_token_store.py (BIS-728 / Slice 0) — same behavior,
proven against the new provider-agnostic implementation, with only the
import path and the (now-required) explicit token_dir argument changed.

Covers:
- _token_to_dict / _dict_to_token: round-trip serialisation, field types
- _token_path: safe filenames, directory traversal prevention, empty user_id
- save_token: creates file, mode 600, overwrites existing, handles bad user_id
- load_token: returns TokenData, returns None on missing file, None on corrupt JSON
- provider_token_dir: fresh-start path shape for providers with no legacy dir
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.oauth import TokenData
from integrations.oauth_vault import vault_store as vs
from integrations.oauth_vault.vault_store import (
    _dict_to_token,
    _token_path,
    _token_to_dict,
    load_token,
    provider_token_dir,
    save_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_ACCESS_TOKEN = "<REDACTED_SECRET>"
_FAKE_REFRESH_TOKEN = "<REDACTED_SECRET>"
_FAKE_SCOPE = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events"

_FUTURE_EXPIRES = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_valid_token(refresh_token: str | None = _FAKE_REFRESH_TOKEN) -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=_FUTURE_EXPIRES,
        scope=_FAKE_SCOPE,
        refresh_token=refresh_token,
    )


# ---------------------------------------------------------------------------
# _token_to_dict / _dict_to_token / round trip
# ---------------------------------------------------------------------------


class TestSerializationRoundTrip:
    def test_round_trip_preserves_all_fields(self) -> None:
        original = _make_valid_token()
        restored = _dict_to_token(_token_to_dict(original))
        assert restored.access_token == original.access_token
        assert restored.refresh_token == original.refresh_token
        assert restored.scope == original.scope
        assert abs((restored.expires_at - original.expires_at).total_seconds()) < 1

    def test_round_trip_with_none_refresh_token(self) -> None:
        original = _make_valid_token(refresh_token=None)
        restored = _dict_to_token(_token_to_dict(original))
        assert restored.refresh_token is None

    def test_naive_expires_at_gets_utc_tzinfo(self) -> None:
        data = {
            "access_token": _FAKE_ACCESS_TOKEN,
            "refresh_token": _FAKE_REFRESH_TOKEN,
            "expires_at": "2099-01-01T00:00:00",  # naive
            "scope": _FAKE_SCOPE,
        }
        result = _dict_to_token(data)
        assert result.expires_at.tzinfo == timezone.utc

    def test_raises_key_error_on_missing_access_token(self) -> None:
        data = {"expires_at": _FUTURE_EXPIRES.isoformat(), "scope": _FAKE_SCOPE}
        with pytest.raises(KeyError):
            _dict_to_token(data)

    def test_raises_value_error_on_invalid_expires_at(self) -> None:
        data = {
            "access_token": _FAKE_ACCESS_TOKEN,
            "expires_at": "not-a-datetime",
            "scope": _FAKE_SCOPE,
        }
        with pytest.raises(ValueError):
            _dict_to_token(data)


# ---------------------------------------------------------------------------
# _token_path
# ---------------------------------------------------------------------------


class TestTokenPath:
    def test_returns_path_object(self, tmp_path: Path) -> None:
        result = _token_path("user123", tmp_path)
        assert isinstance(result, Path)

    def test_filename_is_user_id_dot_json(self, tmp_path: Path) -> None:
        result = _token_path("user123", tmp_path)
        assert result.name == "user123.json"

    def test_parent_is_token_dir(self, tmp_path: Path) -> None:
        result = _token_path("user123", tmp_path)
        assert result.parent == tmp_path

    def test_sanitises_alphanumeric_with_hyphens_and_underscores(self, tmp_path: Path) -> None:
        result = _token_path("user-123_abc", tmp_path)
        assert result.name == "user-123_abc.json"

    def test_strips_path_separator_from_user_id(self, tmp_path: Path) -> None:
        result = _token_path("../evil", tmp_path)
        assert "/" not in result.name
        assert ".." not in result.name

    def test_raises_value_error_on_empty_user_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            _token_path("", tmp_path)

    def test_raises_value_error_on_all_special_chars(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            _token_path("../.", tmp_path)

    def test_telegram_chat_id_is_valid(self, tmp_path: Path) -> None:
        result = _token_path("1234567890", tmp_path)
        assert result.name == "1234567890.json"


# ---------------------------------------------------------------------------
# save_token
# ---------------------------------------------------------------------------


class TestSaveToken:
    def test_creates_token_file(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, tmp_path)
        assert (tmp_path / "user1.json").exists()

    def test_token_file_is_valid_json(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, tmp_path)
        data = json.loads((tmp_path / "user1.json").read_text())
        assert "access_token" in data

    def test_token_file_permissions_are_600(self, tmp_path: Path) -> None:
        token = _make_valid_token()
        save_token("user1", token, tmp_path)
        mode = stat.S_IMODE((tmp_path / "user1.json").stat().st_mode)
        assert mode == 0o600

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        save_token("user1", _make_valid_token(), tmp_path)
        token_b = TokenData(
            access_token="<REDACTED_SECRET>",
            expires_at=_FUTURE_EXPIRES,
            scope=_FAKE_SCOPE,
            refresh_token=_FAKE_REFRESH_TOKEN,
        )
        save_token("user1", token_b, tmp_path)
        data = json.loads((tmp_path / "user1.json").read_text())
        assert data["access_token"] == "<REDACTED_SECRET>"

    def test_creates_token_dir_if_absent(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        save_token("user1", _make_valid_token(), nested)
        assert (nested / "user1.json").exists()

    def test_raises_value_error_on_bad_user_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_token("../.", _make_valid_token(), tmp_path)

    def test_tmp_file_removed_and_exception_propagates_on_rename_failure(
        self, tmp_path: Path
    ) -> None:
        with patch(f"{vs.__name__}.os.rename", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                save_token("user1", _make_valid_token(), tmp_path)
        assert not (tmp_path / "user1.json.tmp").exists()
        assert not (tmp_path / "user1.json").exists()


# ---------------------------------------------------------------------------
# load_token
# ---------------------------------------------------------------------------


class TestLoadToken:
    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        assert load_token("nonexistent", tmp_path) is None

    def test_returns_token_data_after_save(self, tmp_path: Path) -> None:
        save_token("user1", _make_valid_token(), tmp_path)
        result = load_token("user1", tmp_path)
        assert isinstance(result, TokenData)
        assert result.access_token == _FAKE_ACCESS_TOKEN
        assert result.refresh_token == _FAKE_REFRESH_TOKEN

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        (tmp_path / "user1.json").write_text("{ not valid json }")
        assert load_token("user1", tmp_path) is None

    def test_returns_none_on_missing_required_field(self, tmp_path: Path) -> None:
        (tmp_path / "user1.json").write_text(
            json.dumps({"expires_at": _FUTURE_EXPIRES.isoformat()})
        )
        assert load_token("user1", tmp_path) is None

    def test_returns_none_on_invalid_expires_at(self, tmp_path: Path) -> None:
        (tmp_path / "user1.json").write_text(json.dumps({
            "access_token": _FAKE_ACCESS_TOKEN,
            "expires_at": "not-a-date",
            "scope": _FAKE_SCOPE,
        }))
        assert load_token("user1", tmp_path) is None

    def test_loaded_expires_at_is_timezone_aware(self, tmp_path: Path) -> None:
        save_token("user1", _make_valid_token(), tmp_path)
        result = load_token("user1", tmp_path)
        assert result is not None
        assert result.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# provider_token_dir
# ---------------------------------------------------------------------------


class TestProviderTokenDir:
    def test_returns_vault_root_slash_provider(self, tmp_path: Path) -> None:
        result = provider_token_dir("some_new_provider", vault_root=tmp_path)
        assert result == tmp_path / "some_new_provider"

    def test_different_providers_get_different_dirs(self, tmp_path: Path) -> None:
        a = provider_token_dir("provider_a", vault_root=tmp_path)
        b = provider_token_dir("provider_b", vault_root=tmp_path)
        assert a != b


# ---------------------------------------------------------------------------
# Identity metadata (issue #2153) -- email captured at grant time survives
# save/load, consistently with the three predecessor token_store.py modules.
# ---------------------------------------------------------------------------


class TestEmailIdentityMetadata:
    def test_roundtrip_preserves_email(self, tmp_path):
        token = TokenData(
            access_token="tok",
            expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            scope="s",
            refresh_token="r",
            email="account-a@example.com",
        )
        save_token("chat_a", token, tmp_path)
        loaded = load_token("chat_a", tmp_path)
        assert loaded.email == "account-a@example.com"

    def test_missing_email_key_loads_as_none(self, tmp_path):
        (tmp_path / "legacy_user.json").write_text(
            json.dumps(
                {
                    "access_token": "tok",
                    "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
                    "scope": "s",
                    "refresh_token": "r",
                }
            )
        )
        loaded = load_token("legacy_user", tmp_path)
        assert loaded is not None
        assert loaded.email is None

    def test_two_chat_ids_store_independent_emails(self, tmp_path):
        """The exact scenario from the production incident: two different
        chat_ids must each retain their OWN email, with no cross-contamination."""
        token_a = TokenData(
            access_token="tok-a", expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            scope="s", refresh_token="r", email="account-a@example.com",
        )
        token_b = TokenData(
            access_token="tok-b", expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            scope="s", refresh_token="r", email="account-b@example.com",
        )
        save_token("1111111111", token_a, tmp_path)
        save_token("2222222222", token_b, tmp_path)

        assert load_token("1111111111", tmp_path).email == "account-a@example.com"
        assert load_token("2222222222", tmp_path).email == "account-b@example.com"
