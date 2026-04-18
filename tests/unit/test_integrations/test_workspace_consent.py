"""
Tests for Google Workspace support in generate_consent_link.

Covers:
- generate_consent_link("workspace") returns a URL
- generate_consent_link("workspace") sends the correct scope in the JSON payload
- "workspace" is in _VALID_SCOPES
- "drive" and other non-workspace scopes are still rejected
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_auth.consent import _VALID_SCOPES, generate_consent_link

_FAKE_INSTANCE_URL = "https://vps.example.com"
_FAKE_SECRET = "test-internal-secret-abc123"
_FAKE_WORKSPACE_URL = "https://myownlobster.ai/connect/workspace?token=test-uuid"


def _mock_response(url: str) -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 201
    resp.json.return_value = {"url": url}
    resp.text = f'{{"url": "{url}"}}'
    return resp


def _env() -> dict:
    return {
        "LOBSTER_INSTANCE_URL": _FAKE_INSTANCE_URL,
        "LOBSTER_INTERNAL_SECRET": _FAKE_SECRET,
    }


class TestWorkspaceScopeInValidScopes:
    def test_workspace_is_in_valid_scopes(self):
        assert "workspace" in _VALID_SCOPES

    def test_calendar_still_in_valid_scopes(self):
        assert "calendar" in _VALID_SCOPES

    def test_gmail_still_in_valid_scopes(self):
        assert "gmail" in _VALID_SCOPES

    def test_drive_not_in_valid_scopes(self):
        assert "drive" not in _VALID_SCOPES


class TestGenerateConsentLinkWorkspace:
    def test_workspace_scope_returns_url(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response(_FAKE_WORKSPACE_URL),
        ) as mock_post:
            result = generate_consent_link("workspace")

        assert result == _FAKE_WORKSPACE_URL
        mock_post.assert_called_once()

    def test_workspace_scope_sends_correct_payload(self):
        with patch.dict("os.environ", _env()), patch(
            "integrations.google_auth.consent.requests.post",
            return_value=_mock_response(_FAKE_WORKSPACE_URL),
        ) as mock_post:
            generate_consent_link("workspace")

        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or (call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
        if payload is None:
            payload = call_kwargs.kwargs["json"]
        assert payload["scope"] == "workspace"

    def test_invalid_scope_still_rejected(self):
        with patch.dict("os.environ", _env()):
            with pytest.raises(ValueError, match="Invalid scope"):
                generate_consent_link("sheets")
