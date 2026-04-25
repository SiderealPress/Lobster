"""
Tests for gsheets_write and gsheets_create in
src/integrations/google_workspace/sheets_client.py (Slice 6).

Covers:
- gsheets_write: returns True on success
- gsheets_write: returns False when no valid token
- gsheets_write: returns False on API error
- gsheets_write: returns False on network error
- gsheets_write: accepts full Sheets URL
- gsheets_write: sends correct PUT payload with valueInputOption
- gsheets_create: returns DriveFile on success
- gsheets_create: returns None when no valid token
- gsheets_create: returns None on API error
- gsheets_create: returns None on network error
- gsheets_create: returns None when response missing spreadsheetId
- gsheets_create: URL in returned DriveFile contains the spreadsheet ID
- No token values appear in logs
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.oauth import TokenData
from integrations.google_workspace.drive_client import DriveFile
from integrations.google_workspace.sheets_client import (
    gsheets_create,
    gsheets_write,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_ACCESS_TOKEN = "ya29.fake-sheets-token"
_FAKE_SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
_FAKE_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{_FAKE_SHEET_ID}/edit"
_FAKE_SHEET_TITLE = "My Budget Sheet"


def _valid_token() -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="https://www.googleapis.com/auth/spreadsheets",
        refresh_token="test-refresh",
    )


def _write_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "spreadsheetId": _FAKE_SHEET_ID,
        "updatedRange": "A1:C2",
        "updatedRows": 2,
        "updatedColumns": 3,
        "updatedCells": 6,
    }
    return resp


def _create_response(
    sheet_id: str = _FAKE_SHEET_ID,
    title: str = _FAKE_SHEET_TITLE,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "spreadsheetId": sheet_id,
        "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        "properties": {"title": title},
    }
    return resp


def _error_response(status_code: int = 500) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = False
    resp.json.return_value = {}
    return resp


# ---------------------------------------------------------------------------
# gsheets_write
# ---------------------------------------------------------------------------


class TestGsheetsWrite:
    _sample_values = [["col1", "col2"], ["a", "b"], ["c", "d"]]

    def test_returns_true_on_success(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.put",
            return_value=_write_response(),
        ):
            result = gsheets_write("user123", _FAKE_SHEET_ID, "A1:C3", self._sample_values)

        assert result is True

    def test_returns_false_when_no_token(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=None,
        ):
            result = gsheets_write("user123", _FAKE_SHEET_ID, "A1:C3", self._sample_values)

        assert result is False

    def test_returns_false_on_api_error(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.put",
            return_value=_error_response(403),
        ):
            result = gsheets_write("user123", _FAKE_SHEET_ID, "A1:C3", self._sample_values)

        assert result is False

    def test_returns_false_on_network_error(self):
        import requests as req_lib

        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.put",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            result = gsheets_write("user123", _FAKE_SHEET_ID, "A1:C3", self._sample_values)

        assert result is False

    def test_accepts_full_url(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.put",
            return_value=_write_response(),
        ) as mock_put:
            result = gsheets_write("user123", _FAKE_SHEET_URL, "A1:C3", self._sample_values)

        assert result is True
        called_url = mock_put.call_args[0][0]
        assert _FAKE_SHEET_ID in called_url
        assert "spreadsheets/d" not in called_url  # full URL not used as path

    def test_sends_user_entered_value_input_option(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.put",
            return_value=_write_response(),
        ) as mock_put:
            gsheets_write("user123", _FAKE_SHEET_ID, "A1:C3", self._sample_values)

        call_kwargs = mock_put.call_args
        params = call_kwargs[1].get("params") or call_kwargs.kwargs.get("params", {})
        assert params.get("valueInputOption") == "USER_ENTERED"

    def test_sends_values_in_request_body(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.put",
            return_value=_write_response(),
        ) as mock_put:
            gsheets_write("user123", _FAKE_SHEET_ID, "A1:C3", self._sample_values)

        call_kwargs = mock_put.call_args
        json_body = call_kwargs[1].get("json") or call_kwargs.kwargs.get("json", {})
        assert json_body.get("values") == self._sample_values
        assert json_body.get("majorDimension") == "ROWS"


# ---------------------------------------------------------------------------
# gsheets_create
# ---------------------------------------------------------------------------


class TestGsheetsCreate:
    def test_returns_drivefile_on_success(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            return_value=_create_response(),
        ):
            result = gsheets_create("user123", _FAKE_SHEET_TITLE)

        assert result is not None
        assert isinstance(result, DriveFile)
        assert result.id == _FAKE_SHEET_ID
        assert result.name == _FAKE_SHEET_TITLE

    def test_url_contains_sheet_id(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            return_value=_create_response(),
        ):
            result = gsheets_create("user123", _FAKE_SHEET_TITLE)

        assert result is not None
        assert _FAKE_SHEET_ID in result.url

    def test_mime_type_is_spreadsheet(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            return_value=_create_response(),
        ):
            result = gsheets_create("user123", _FAKE_SHEET_TITLE)

        assert result is not None
        assert result.mime_type == "application/vnd.google-apps.spreadsheet"

    def test_returns_none_when_no_token(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=None,
        ):
            result = gsheets_create("user123", _FAKE_SHEET_TITLE)

        assert result is None

    def test_returns_none_on_api_error(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            return_value=_error_response(403),
        ):
            result = gsheets_create("user123", _FAKE_SHEET_TITLE)

        assert result is None

    def test_returns_none_on_network_error(self):
        import requests as req_lib

        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            result = gsheets_create("user123", _FAKE_SHEET_TITLE)

        assert result is None

    def test_returns_none_when_response_missing_spreadsheet_id(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = {"properties": {"title": _FAKE_SHEET_TITLE}}  # missing id

        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            return_value=resp,
        ):
            result = gsheets_create("user123", _FAKE_SHEET_TITLE)

        assert result is None

    def test_sends_title_in_request_body(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            return_value=_create_response(),
        ) as mock_post:
            gsheets_create("user123", "Special Spreadsheet")

        call_kwargs = mock_post.call_args
        json_body = call_kwargs[1].get("json") or call_kwargs.kwargs.get("json", {})
        assert json_body.get("properties", {}).get("title") == "Special Spreadsheet"


# ---------------------------------------------------------------------------
# No token values in logs
# ---------------------------------------------------------------------------


def test_access_token_not_logged_in_write(caplog):
    import logging

    with caplog.at_level(
        logging.DEBUG, logger="integrations.google_workspace.sheets_client"
    ):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.put",
            return_value=_write_response(),
        ):
            gsheets_write("user123", _FAKE_SHEET_ID, "A1:B2", [["x", "y"]])

    for record in caplog.records:
        assert _FAKE_ACCESS_TOKEN not in record.getMessage()


def test_access_token_not_logged_in_create(caplog):
    import logging

    with caplog.at_level(
        logging.DEBUG, logger="integrations.google_workspace.sheets_client"
    ):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.post",
            return_value=_create_response(),
        ):
            gsheets_create("user123", _FAKE_SHEET_TITLE)

    for record in caplog.records:
        assert _FAKE_ACCESS_TOKEN not in record.getMessage()
