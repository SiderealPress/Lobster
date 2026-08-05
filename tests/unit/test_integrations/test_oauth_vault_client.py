"""
Tests for src/integrations/oauth_vault/client.py (BIS-732 / Slice 4).

This is the provider-parameterized get_valid_token()/refresh-proxy layer
extracted from google_calendar/token_store.py (and, in spirit, gmail's and
google_workspace's identical logic). These tests mirror the corresponding
characterization assertions in test_google_calendar_token_store.py
(BIS-728 / Slice 0, extended by BIS-731 / Slice 3's workspace-fallback
tests) — same expected behavior, proven against the new
provider-parameterized implementation with provider="calendar", with only
the import path and the (now-required) explicit provider argument changed.

Covers:
- get_valid_token: returns valid token, refreshes expired token, saves
  refreshed token, carries forward refresh_token when Google omits it,
  returns None on missing token, returns None when refresh fails, returns
  None when no refresh_token
- get_valid_token workspace-token fallback (generalized BIS-731 pattern)
- _refresh_token_via_proxy: HTTP side-effecting boundary
- _internal_auth_header / provider config loader
- Provider registry: unknown provider raises; "calendar" aliases to the
  pre-existing gcal-tokens/ directory (regression test for the deliberate
  deviation documented in client.py's module docstring — protects against
  the read path silently disagreeing with the not-yet-migrated write path)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.oauth import TokenData
from integrations.google_workspace.token_store import save_token as _save_workspace_token
from integrations.oauth_vault import client as ovc
from integrations.oauth_vault.client import get_valid_token
from integrations.oauth_vault.vault_store import load_token, save_token

_PROVIDER = "calendar"

_FAKE_ACCESS_TOKEN = "<REDACTED_SECRET>"
_FAKE_REFRESH_TOKEN = "<REDACTED_SECRET>"
_FAKE_SCOPE = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events"

_FUTURE_EXPIRES = datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_EXPIRED_EXPIRES = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_valid_token(refresh_token: str | None = _FAKE_REFRESH_TOKEN) -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=_FUTURE_EXPIRES,
        scope=_FAKE_SCOPE,
        refresh_token=refresh_token,
    )


def _make_expired_token(refresh_token: str | None = _FAKE_REFRESH_TOKEN) -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=_EXPIRED_EXPIRES,
        scope=_FAKE_SCOPE,
        refresh_token=refresh_token,
    )


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    def test_unknown_provider_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown oauth_vault provider"):
            get_valid_token("not_a_real_provider", "user1", token_dir=tmp_path)

    def test_calendar_provider_aliases_to_legacy_gcal_tokens_dir(self) -> None:
        """Regression test for the deliberate deviation documented in
        client.py: the "calendar" provider's default token_dir must equal
        google_calendar/token_store.py's own _TOKEN_DIR, because that
        module's write path (push_calendar_token_endpoint, callback_server.py)
        is not cut over in this slice and still writes there. If these ever
        diverge, a real Calendar token becomes invisible to the new read
        path — silently breaking Calendar for a live user."""
        from integrations.google_calendar import token_store as legacy_ts

        assert ovc.PROVIDERS["calendar"].token_dir == legacy_ts._TOKEN_DIR


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------


class TestGetValidToken:
    def test_returns_none_when_no_token_file(self, tmp_path: Path) -> None:
        result = get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        assert result is None

    def test_returns_valid_token_without_refresh(self, tmp_path: Path) -> None:
        save_token("user1", _make_valid_token(), tmp_path)
        result = get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        assert result is not None
        assert result.access_token == _FAKE_ACCESS_TOKEN

    def test_refreshes_expired_token(self, tmp_path: Path) -> None:
        save_token("user1", _make_expired_token(), tmp_path)
        new_access = "ya29.refreshed-access-token"
        proxy_result = TokenData(
            access_token=new_access, expires_at=_FUTURE_EXPIRES, scope="", refresh_token=None
        )
        with patch(f"{ovc.__name__}._refresh_token_via_proxy", return_value=proxy_result):
            result = get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        assert result is not None
        assert result.access_token == new_access

    def test_saves_refreshed_token_to_disk(self, tmp_path: Path) -> None:
        save_token("user1", _make_expired_token(), tmp_path)
        new_access = "ya29.refreshed-token"
        proxy_result = TokenData(
            access_token=new_access, expires_at=_FUTURE_EXPIRES, scope="", refresh_token=None
        )
        with patch(f"{ovc.__name__}._refresh_token_via_proxy", return_value=proxy_result):
            get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        stored = load_token("user1", tmp_path)
        assert stored is not None
        assert stored.access_token == new_access

    def test_carries_forward_refresh_token_when_google_omits_it(self, tmp_path: Path) -> None:
        original_refresh = "1//original-refresh-token"
        save_token("user1", _make_expired_token(refresh_token=original_refresh), tmp_path)
        proxy_result = TokenData(
            access_token="<REDACTED_SECRET>", expires_at=_FUTURE_EXPIRES, scope="", refresh_token=None
        )
        with patch(f"{ovc.__name__}._refresh_token_via_proxy", return_value=proxy_result):
            result = get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        assert result is not None
        assert result.refresh_token == original_refresh

    def test_returns_none_when_refresh_proxy_returns_none(self, tmp_path: Path) -> None:
        save_token("user1", _make_expired_token(), tmp_path)
        with patch(f"{ovc.__name__}._refresh_token_via_proxy", return_value=None):
            result = get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        assert result is None

    def test_returns_none_when_token_expired_and_no_refresh_token(self, tmp_path: Path) -> None:
        save_token("user1", _make_expired_token(refresh_token=None), tmp_path)
        result = get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        assert result is None

    def test_does_not_call_refresh_for_valid_token(self, tmp_path: Path) -> None:
        save_token("user1", _make_valid_token(), tmp_path)
        with patch(f"{ovc.__name__}._refresh_token_via_proxy") as mock_proxy:
            get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        mock_proxy.assert_not_called()

    def test_proxy_called_with_provider_and_refresh_token(self, tmp_path: Path) -> None:
        save_token("user1", _make_expired_token(refresh_token=_FAKE_REFRESH_TOKEN), tmp_path)
        proxy_result = TokenData(
            access_token="<REDACTED_SECRET>", expires_at=_FUTURE_EXPIRES, scope="", refresh_token=None
        )
        with patch(
            f"{ovc.__name__}._refresh_token_via_proxy", return_value=proxy_result
        ) as mock_proxy:
            get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)
        mock_proxy.assert_called_once_with(_PROVIDER, _FAKE_REFRESH_TOKEN)

    def test_uses_provider_default_token_dir_when_not_given(self) -> None:
        """When token_dir isn't passed, the provider's registered default is used."""
        with patch(f"{ovc.__name__}._load_token", return_value=None) as mock_load, \
             patch(f"{ovc.__name__}._get_workspace_fallback_token", return_value=None):
            get_valid_token(_PROVIDER, "user1")
        args, _ = mock_load.call_args
        assert args[1] == ovc.PROVIDERS[_PROVIDER].token_dir


# ---------------------------------------------------------------------------
# get_valid_token — workspace-token fallback (generalized BIS-731 pattern)
# ---------------------------------------------------------------------------

_WORKSPACE_SCOPE_WITH_CALENDAR = (
    "https://www.googleapis.com/auth/documents "
    "https://www.googleapis.com/auth/drive "
    "https://www.googleapis.com/auth/spreadsheets "
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/calendar"
)

_WORKSPACE_SCOPE_WITHOUT_CALENDAR = (
    "https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/spreadsheets"
)


def _make_workspace_token(scope: str, refresh_token: str | None = "workspace-refresh-token") -> TokenData:
    return TokenData(
        access_token="workspace-access-token",
        expires_at=_FUTURE_EXPIRES,
        scope=scope,
        refresh_token=refresh_token,
    )


class TestGetValidTokenWorkspaceFallback:
    def test_falls_back_to_workspace_token_when_own_file_absent(self, tmp_path: Path) -> None:
        own_dir = tmp_path / "calendar-tokens"
        workspace_dir = tmp_path / "workspace-tokens"
        _save_workspace_token("user1", _make_workspace_token(_WORKSPACE_SCOPE_WITH_CALENDAR), token_dir=workspace_dir)

        result = get_valid_token(_PROVIDER, "user1", token_dir=own_dir, workspace_token_dir=workspace_dir)

        assert result is not None
        assert result.access_token == "workspace-access-token"

    def test_workspace_fallback_rejected_when_scope_lacks_calendar(self, tmp_path: Path) -> None:
        own_dir = tmp_path / "calendar-tokens"
        workspace_dir = tmp_path / "workspace-tokens"
        _save_workspace_token("user1", _make_workspace_token(_WORKSPACE_SCOPE_WITHOUT_CALENDAR), token_dir=workspace_dir)

        result = get_valid_token(_PROVIDER, "user1", token_dir=own_dir, workspace_token_dir=workspace_dir)

        assert result is None

    def test_own_token_wins_and_workspace_is_never_consulted(self, tmp_path: Path) -> None:
        own_dir = tmp_path / "calendar-tokens"
        workspace_dir = tmp_path / "workspace-tokens"
        own_token = _make_valid_token()
        save_token("user1", own_token, own_dir)

        workspace_dir.mkdir(parents=True)
        (workspace_dir / "user1.json").write_text("{ not valid json }")

        with patch(f"{ovc.__name__}._get_workspace_fallback_token") as mock_fallback:
            result = get_valid_token(_PROVIDER, "user1", token_dir=own_dir, workspace_token_dir=workspace_dir)

        mock_fallback.assert_not_called()
        assert result is not None
        assert result.access_token == own_token.access_token

    def test_returns_none_when_neither_own_nor_workspace_token_exists(self, tmp_path: Path) -> None:
        own_dir = tmp_path / "calendar-tokens"
        workspace_dir = tmp_path / "workspace-tokens"

        result = get_valid_token(_PROVIDER, "user1", token_dir=own_dir, workspace_token_dir=workspace_dir)

        assert result is None

    def test_provider_without_workspace_scope_substring_never_falls_back(self, tmp_path: Path) -> None:
        """A hypothetical provider that declares no workspace_scope_substring
        must never consult the workspace store, even if one exists."""
        own_dir = tmp_path / "no-fallback-tokens"
        workspace_dir = tmp_path / "workspace-tokens"
        _save_workspace_token("user1", _make_workspace_token(_WORKSPACE_SCOPE_WITH_CALENDAR), token_dir=workspace_dir)

        no_fallback_cfg = ovc.ProviderConfig(
            name="no_fallback_test_provider",
            token_dir=own_dir,
            refresh_endpoint="/api/internal/refresh-no-fallback-token",
            config_path=tmp_path / "no-fallback-config.json",
            workspace_scope_substring=None,
        )
        with patch.dict(ovc.PROVIDERS, {"no_fallback_test_provider": no_fallback_cfg}):
            result = get_valid_token(
                "no_fallback_test_provider", "user1", token_dir=own_dir, workspace_token_dir=workspace_dir
            )

        assert result is None


# ---------------------------------------------------------------------------
# _internal_auth_header
# ---------------------------------------------------------------------------


class TestInternalAuthHeader:
    def test_raises_runtime_error_when_secret_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="LOBSTER_INTERNAL_SECRET"):
                ovc._internal_auth_header()

    def test_returns_bearer_header_with_static_secret(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "my-secret"}, clear=True):
            header = ovc._internal_auth_header()
        assert header == {"Authorization": "Bearer my-secret"}

    def test_strips_surrounding_whitespace_from_secret(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "  my-secret  "}, clear=True):
            header = ovc._internal_auth_header()
        assert header == {"Authorization": "Bearer my-secret"}


# ---------------------------------------------------------------------------
# Provider config loader (myownlobster_api_base override)
# ---------------------------------------------------------------------------


class TestProviderConfigLoader:
    def test_returns_default_api_base_when_config_absent(self, tmp_path: Path) -> None:
        cfg = ovc.ProviderConfig(
            name="test", token_dir=tmp_path, refresh_endpoint="/x", config_path=tmp_path / "missing.json"
        )
        assert ovc._myownlobster_api_base(cfg) == "https://myownlobster.ai"

    def test_returns_configured_api_base(self, tmp_path: Path) -> None:
        config_path = tmp_path / "provider-config.json"
        config_path.write_text(json.dumps({"myownlobster_api_base": "https://custom.example/"}))
        cfg = ovc.ProviderConfig(
            name="test", token_dir=tmp_path, refresh_endpoint="/x", config_path=config_path
        )
        assert ovc._myownlobster_api_base(cfg) == "https://custom.example"

    def test_returns_default_on_malformed_json(self, tmp_path: Path) -> None:
        config_path = tmp_path / "provider-config.json"
        config_path.write_text("{ not valid json }")
        cfg = ovc.ProviderConfig(
            name="test", token_dir=tmp_path, refresh_endpoint="/x", config_path=config_path
        )
        assert ovc._myownlobster_api_base(cfg) == "https://myownlobster.ai"


# ---------------------------------------------------------------------------
# _refresh_token_via_proxy — HTTP side-effecting boundary
# ---------------------------------------------------------------------------


class TestRefreshTokenViaProxy:
    def _mock_success_response(self) -> MagicMock:
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"access_token": "new-access-token", "expires_in": 3600}
        return resp

    def test_returns_new_token_on_success(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=self._mock_success_response()
        ):
            result = ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert result is not None
        assert result.access_token == "new-access-token"
        assert result.expires_at > datetime.now(tz=timezone.utc)

    def test_new_token_has_empty_scope_and_no_refresh_token(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=self._mock_success_response()
        ):
            result = ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert result is not None
        assert result.scope == ""
        assert result.refresh_token is None

    def test_returns_none_when_secret_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert result is None

    def test_returns_none_on_network_error(self) -> None:
        import requests as req_lib

        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", side_effect=req_lib.exceptions.ConnectionError("refused")
        ):
            result = ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert result is None

    def test_returns_none_on_non_ok_response(self) -> None:
        bad = MagicMock()
        bad.ok = False
        bad.status_code = 500
        bad.text = "Internal Server Error"
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=bad
        ):
            result = ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert result is None

    def test_returns_none_on_bad_json(self) -> None:
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"unexpected": "keys"}
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=resp
        ):
            result = ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert result is None

    def test_uses_calendar_refresh_endpoint_for_calendar_provider(self) -> None:
        """Refresh must call the calendar-specific endpoint, not gmail's —
        proving provider-parameterization actually dispatches correctly."""
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=self._mock_success_response()
        ) as mock_post:
            ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        called_url = mock_post.call_args.args[0]
        assert called_url == "https://myownlobster.ai/api/internal/refresh-calendar-token"

    def test_posts_refresh_token_in_json_body(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=self._mock_success_response()
        ) as mock_post:
            ovc._refresh_token_via_proxy(_PROVIDER, "the-refresh-token")
        assert mock_post.call_args.kwargs["json"] == {"refresh_token": "the-refresh-token"}

    def test_sends_bearer_auth_header(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "topsecret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=self._mock_success_response()
        ) as mock_post:
            ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert mock_post.call_args.kwargs["headers"] == {"Authorization": "Bearer topsecret"}

    def test_uses_10_second_timeout(self) -> None:
        with patch.dict(os.environ, {"LOBSTER_INTERNAL_SECRET": "secret"}, clear=True), patch(
            f"{ovc.__name__}.requests.post", return_value=self._mock_success_response()
        ) as mock_post:
            ovc._refresh_token_via_proxy(_PROVIDER, "refresh-tok")
        assert mock_post.call_args.kwargs["timeout"] == 10

    def test_raises_for_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown oauth_vault provider"):
            ovc._refresh_token_via_proxy("not_a_real_provider", "refresh-tok")


# ---------------------------------------------------------------------------
# Identity metadata (issue #2153) -- email is preserved across a refresh,
# consistently with the three predecessor token_store.py modules'
# get_valid_token behavior.
# ---------------------------------------------------------------------------


class TestEmailIdentityMetadata:
    def test_email_preserved_across_refresh(self, tmp_path: Path) -> None:
        expired = TokenData(
            access_token=_FAKE_ACCESS_TOKEN,
            expires_at=_EXPIRED_EXPIRES,
            scope=_FAKE_SCOPE,
            refresh_token=_FAKE_REFRESH_TOKEN,
            email="account-a@example.com",
        )
        save_token("user1", expired, tmp_path)

        proxy_result = TokenData(
            access_token="ya29.refreshed", expires_at=_FUTURE_EXPIRES, scope="", refresh_token=None,
        )
        with patch(f"{ovc.__name__}._refresh_token_via_proxy", return_value=proxy_result):
            result = get_valid_token(_PROVIDER, "user1", token_dir=tmp_path)

        assert result.email == "account-a@example.com"
        assert load_token("user1", tmp_path).email == "account-a@example.com"
