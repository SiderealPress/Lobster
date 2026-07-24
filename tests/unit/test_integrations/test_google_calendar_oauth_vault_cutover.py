"""
Tests proving google_calendar/client.py actually delegates to oauth_vault
(BIS-732 / Slice 4), not just that the two happen to behave similarly.

The existing test_google_calendar_client.py suite patches
`integrations.google_calendar.client.get_valid_token` directly, so it can't
tell us whether that name is backed by oauth_vault or still by the old
token_store.py — it would pass either way. These tests close that gap by
patching one level deeper, at `integrations.oauth_vault.client.get_valid_token`,
and confirming google_calendar.client's wrapper calls through to it with
provider="calendar".
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.client import get_valid_token
from integrations.google_calendar.oauth import TokenData

_FAKE_TOKEN = TokenData(
    access_token="tok",
    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    scope="https://www.googleapis.com/auth/calendar",
    refresh_token="refresh",
)


class TestCutoverDelegatesToOAuthVault:
    def test_delegates_to_oauth_vault_with_calendar_provider(self) -> None:
        with patch(
            "integrations.oauth_vault.client.get_valid_token", return_value=_FAKE_TOKEN
        ) as mock_vault_gvt:
            result = get_valid_token("user1", credentials="fake-creds")

        mock_vault_gvt.assert_called_once_with("calendar", "user1", credentials="fake-creds")
        assert result is _FAKE_TOKEN

    def test_credentials_default_none_forwarded(self) -> None:
        with patch(
            "integrations.oauth_vault.client.get_valid_token", return_value=None
        ) as mock_vault_gvt:
            get_valid_token("user1")

        mock_vault_gvt.assert_called_once_with("calendar", "user1", credentials=None)

    def test_old_token_store_module_is_no_longer_imported_by_client(self) -> None:
        """The old google_calendar/token_store.py must stay in the tree
        (rollback path) but must no longer be imported by client.py — this
        is what makes the cutover real rather than cosmetic."""
        import integrations.google_calendar.client as gcal_client

        assert not hasattr(gcal_client, "token_store")
        # The module-level get_valid_token symbol exists, but must not be
        # the same function object as the old token_store's.
        from integrations.google_calendar import token_store as legacy_ts

        assert gcal_client.get_valid_token is not legacy_ts.get_valid_token
