"""
Tests for gsheets_read in src/integrations/google_workspace/sheets_client.py (Slice 5).

Covers:
- gsheets_read: returns 2D list on success
- gsheets_read: returns [] when no valid token
- gsheets_read: returns [] on API error
- gsheets_read: returns [] on network error
- gsheets_read: empty cells become empty strings
- gsheets_read: accepts full Sheets URL
- _sheet_id_from_url: extracts ID from full URL
- _sheet_id_from_url: returns raw string if not a URL
- _normalise_row: converts non-string values to strings
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
from integrations.google_workspace.sheets_client import (
    _normalise_row,
    _sheet_id_from_url,
    gsheets_read,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_ACCESS_TOKEN = "ya29.fake-sheets-token"
_FAKE_SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
_FAKE_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{_FAKE_SHEET_ID}/edit"


def _valid_token() -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="https://www.googleapis.com/auth/spreadsheets",
        refresh_token="test-refresh",
    )


def _sheets_response(values: list) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {"values": values, "range": "A1:C3"}
    return resp


def _error_response(status_code: int = 500) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = False
    resp.json.return_value = {}
    return resp


# ---------------------------------------------------------------------------
# _sheet_id_from_url — pure function tests
# ---------------------------------------------------------------------------


class TestSheetIdFromUrl:
    def test_extracts_id_from_full_edit_url(self):
        result = _sheet_id_from_url(_FAKE_SHEET_URL)
        assert result == _FAKE_SHEET_ID

    def test_extracts_id_from_view_url(self):
        url = f"https://docs.google.com/spreadsheets/d/{_FAKE_SHEET_ID}/view"
        result = _sheet_id_from_url(url)
        assert result == _FAKE_SHEET_ID

    def test_returns_raw_id_when_no_url_match(self):
        result = _sheet_id_from_url(_FAKE_SHEET_ID)
        assert result == _FAKE_SHEET_ID

    def test_strips_whitespace_from_raw_id(self):
        result = _sheet_id_from_url(f"  {_FAKE_SHEET_ID}  ")
        assert result == _FAKE_SHEET_ID


# ---------------------------------------------------------------------------
# _normalise_row — pure function tests
# ---------------------------------------------------------------------------


class TestNormaliseRow:
    def test_strings_pass_through(self):
        result = _normalise_row(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_numbers_become_strings(self):
        result = _normalise_row([1, 2.5, 3])
        assert result == ["1", "2.5", "3"]

    def test_none_becomes_empty_string(self):
        result = _normalise_row([None, "x", None])
        assert result == ["", "x", ""]

    def test_empty_row_returns_empty_list(self):
        result = _normalise_row([])
        assert result == []


# ---------------------------------------------------------------------------
# gsheets_read — integration tests (mocked)
# ---------------------------------------------------------------------------


class TestGsheetsRead:
    def test_returns_2d_list_on_success(self):
        values = [["col1", "col2", "col3"], ["a", "b", "c"], ["d", "e", "f"]]
        http_resp = _sheets_response(values)

        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.get",
            return_value=http_resp,
        ):
            result = gsheets_read("user123", _FAKE_SHEET_ID, "A1:C3")

        assert result == [["col1", "col2", "col3"], ["a", "b", "c"], ["d", "e", "f"]]

    def test_returns_empty_list_when_no_token(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=None,
        ):
            result = gsheets_read("user123", _FAKE_SHEET_ID, "A1:C3")

        assert result == []

    def test_returns_empty_list_on_api_error(self):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.get",
            return_value=_error_response(403),
        ):
            result = gsheets_read("user123", _FAKE_SHEET_ID, "A1:C3")

        assert result == []

    def test_returns_empty_list_on_network_error(self):
        import requests as req_lib
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.get",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            result = gsheets_read("user123", _FAKE_SHEET_ID, "A1:C3")

        assert result == []

    def test_returns_empty_list_when_no_data_in_range(self):
        # API returns empty values when range has no data
        http_resp = MagicMock()
        http_resp.status_code = 200
        http_resp.ok = True
        http_resp.json.return_value = {}  # no "values" key when range is empty

        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.get",
            return_value=http_resp,
        ):
            result = gsheets_read("user123", _FAKE_SHEET_ID, "Z1:Z10")

        assert result == []

    def test_accepts_full_url(self):
        values = [["x"]]
        http_resp = _sheets_response(values)

        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.get",
            return_value=http_resp,
        ) as mock_get:
            gsheets_read("user123", _FAKE_SHEET_URL, "A1:A1")

        # Verify the called URL contains the extracted sheet ID
        called_url = mock_get.call_args[0][0]
        assert _FAKE_SHEET_ID in called_url
        assert "spreadsheets/d" not in called_url  # full URL not used as path

    def test_all_cells_are_strings(self):
        # API may return numbers as native JSON types
        values = [[1, 2.5, None, "text"]]
        http_resp = _sheets_response(values)

        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.get",
            return_value=http_resp,
        ):
            result = gsheets_read("user123", _FAKE_SHEET_ID, "A1:D1")

        assert result == [["1", "2.5", "", "text"]]


# ---------------------------------------------------------------------------
# No token values in logs
# ---------------------------------------------------------------------------


def test_access_token_not_logged(caplog):
    import logging
    http_resp = _sheets_response([["data"]])

    with caplog.at_level(logging.DEBUG, logger="integrations.google_workspace.sheets_client"):
        with patch(
            "integrations.google_workspace.sheets_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.sheets_client.requests.get",
            return_value=http_resp,
        ):
            gsheets_read("user123", _FAKE_SHEET_ID, "A1:B2")

    for record in caplog.records:
        assert _FAKE_ACCESS_TOKEN not in record.getMessage()
