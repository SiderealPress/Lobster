"""
Tests for src/integrations/google_workspace/drive_client.py (Slice 4).

Covers:
- gdrive_list: returns DriveFile list on success
- gdrive_list: returns [] when no valid token
- gdrive_list: returns [] on API error
- gdrive_list: returns [] on network error
- gdrive_search: returns matching files
- gdrive_search: returns [] when no results
- gdrive_search: appends trashed=false to query
- _parse_drive_file: parses a valid item correctly
- _parse_drive_file: returns None for missing id or name
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
from integrations.google_workspace.drive_client import (
    DriveFile,
    _parse_drive_file,
    gdrive_list,
    gdrive_search,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_ACCESS_TOKEN = "ya29.fake-drive-token"


def _valid_token() -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="https://www.googleapis.com/auth/drive",
        refresh_token="test-refresh",
    )


def _drive_item(
    file_id: str = "abc123",
    name: str = "Test Doc",
    mime_type: str = "application/vnd.google-apps.document",
    url: str = "https://docs.google.com/document/d/abc123/edit",
    modified_time: str = "2026-04-18T10:00:00Z",
) -> dict:
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "webViewLink": url,
        "modifiedTime": modified_time,
    }


def _http_response(items: list, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.json.return_value = {"files": items}
    return resp


def _error_response(status_code: int = 500) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = False
    resp.json.return_value = {"error": {"code": status_code}}
    return resp


# ---------------------------------------------------------------------------
# _parse_drive_file — pure function tests
# ---------------------------------------------------------------------------


class TestParseDriveFile:
    def test_parses_valid_item(self):
        item = _drive_item()
        result = _parse_drive_file(item)
        assert result is not None
        assert result.id == "abc123"
        assert result.name == "Test Doc"
        assert result.mime_type == "application/vnd.google-apps.document"
        assert result.url == "https://docs.google.com/document/d/abc123/edit"
        assert isinstance(result.modified_time, datetime)
        assert result.modified_time.tzinfo is not None

    def test_returns_none_for_missing_id(self):
        item = _drive_item(file_id="")
        result = _parse_drive_file(item)
        assert result is None

    def test_returns_none_for_missing_name(self):
        item = _drive_item(name="")
        result = _parse_drive_file(item)
        assert result is None

    def test_handles_missing_modified_time(self):
        item = _drive_item()
        del item["modifiedTime"]
        result = _parse_drive_file(item)
        assert result is not None
        assert result.modified_time.tzinfo is not None  # falls back to now()


# ---------------------------------------------------------------------------
# gdrive_list
# ---------------------------------------------------------------------------


class TestGdriveList:
    def test_returns_list_on_success(self):
        items = [_drive_item("id1", "Doc 1"), _drive_item("id2", "Doc 2")]
        http_resp = _http_response(items)

        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=http_resp,
        ):
            result = gdrive_list("user123")

        assert len(result) == 2
        assert all(isinstance(f, DriveFile) for f in result)
        assert result[0].id == "id1"
        assert result[1].id == "id2"

    def test_returns_empty_list_when_no_token(self):
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=None,
        ):
            result = gdrive_list("user123")

        assert result == []

    def test_returns_empty_list_on_api_error(self):
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=_error_response(500),
        ):
            result = gdrive_list("user123")

        assert result == []

    def test_returns_empty_list_on_network_error(self):
        import requests as req_lib
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            result = gdrive_list("user123")

        assert result == []

    def test_returns_empty_list_on_empty_results(self):
        http_resp = _http_response([])
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=http_resp,
        ):
            result = gdrive_list("user123")

        assert result == []

    def test_uses_folder_id_in_query(self):
        http_resp = _http_response([])
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=http_resp,
        ) as mock_get:
            gdrive_list("user123", folder_id="specific-folder-id")

        params = mock_get.call_args.kwargs.get("params") or mock_get.call_args[1].get("params", {})
        assert "specific-folder-id" in params.get("q", "")


# ---------------------------------------------------------------------------
# gdrive_search
# ---------------------------------------------------------------------------


class TestGdriveSearch:
    def test_returns_matching_files(self):
        items = [_drive_item("search1", "Search Result")]
        http_resp = _http_response(items)

        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=http_resp,
        ):
            result = gdrive_search("user123", "name contains 'Search'")

        assert len(result) == 1
        assert result[0].name == "Search Result"

    def test_returns_empty_list_when_no_results(self):
        http_resp = _http_response([])
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=http_resp,
        ):
            result = gdrive_search("user123", "name contains 'nonexistent'")

        assert result == []

    def test_appends_trashed_false_to_query(self):
        http_resp = _http_response([])
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=http_resp,
        ) as mock_get:
            gdrive_search("user123", "name contains 'test'")

        params = mock_get.call_args.kwargs.get("params") or mock_get.call_args[1].get("params", {})
        assert "trashed=false" in params.get("q", "")

    def test_returns_empty_list_when_no_token(self):
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=None,
        ):
            result = gdrive_search("user123", "name contains 'test'")

        assert result == []


# ---------------------------------------------------------------------------
# No token values in logs
# ---------------------------------------------------------------------------


def test_access_token_not_logged(caplog):
    import logging
    http_resp = _http_response([_drive_item()])

    with caplog.at_level(logging.DEBUG, logger="integrations.google_workspace.drive_client"):
        with patch(
            "integrations.google_workspace.drive_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.drive_client.requests.get",
            return_value=http_resp,
        ):
            gdrive_list("user123")
            gdrive_search("user123", "name contains 'test'")

    for record in caplog.records:
        assert _FAKE_ACCESS_TOKEN not in record.getMessage()
