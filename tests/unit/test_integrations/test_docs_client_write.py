"""
Tests for gdocs_create and gdocs_edit in
src/integrations/google_workspace/docs_client.py (Slice 3).

Covers:
- gdocs_create: returns DocFile on success
- gdocs_create: returns None when no valid token
- gdocs_create: returns None on API error
- gdocs_create: returns None on network error
- gdocs_create: returns None when API response missing documentId
- gdocs_create: URL in returned DocFile contains the document ID
- gdocs_edit: returns True on success
- gdocs_edit: returns False when no valid token
- gdocs_edit: returns False on API error
- gdocs_edit: returns False on network error
- gdocs_edit: accepts full URL as doc_id_or_url
- gdocs_edit: sends requests list in correct batchUpdate payload
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
from integrations.google_workspace.docs_client import (
    DocFile,
    _doc_id_from_url,
    gdocs_create,
    gdocs_edit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_ACCESS_TOKEN = "ya29.fake-workspace-token"
_FAKE_DOC_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
_FAKE_DOC_URL = f"https://docs.google.com/document/d/{_FAKE_DOC_ID}/edit"
_FAKE_DOC_TITLE = "My Test Document"


def _valid_token() -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="https://www.googleapis.com/auth/documents",
        refresh_token="test-refresh",
    )


def _create_response(
    doc_id: str = _FAKE_DOC_ID,
    title: str = _FAKE_DOC_TITLE,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {
        "documentId": doc_id,
        "title": title,
    }
    return resp


def _batch_update_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = {"documentId": _FAKE_DOC_ID, "replies": [{}]}
    return resp


def _error_response(status_code: int = 500) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = False
    resp.json.return_value = {}
    return resp


# ---------------------------------------------------------------------------
# gdocs_create
# ---------------------------------------------------------------------------


class TestGdocsCreate:
    def test_returns_docfile_on_success(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_create_response(),
        ):
            result = gdocs_create("user123", _FAKE_DOC_TITLE)

        assert result is not None
        assert isinstance(result, DocFile)
        assert result.id == _FAKE_DOC_ID
        assert result.title == _FAKE_DOC_TITLE

    def test_url_contains_doc_id(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_create_response(),
        ):
            result = gdocs_create("user123", _FAKE_DOC_TITLE)

        assert result is not None
        assert _FAKE_DOC_ID in result.url
        assert "docs.google.com/document/d/" in result.url

    def test_returns_none_when_no_token(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=None,
        ):
            result = gdocs_create("user123", _FAKE_DOC_TITLE)

        assert result is None

    def test_returns_none_on_api_error(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_error_response(403),
        ):
            result = gdocs_create("user123", _FAKE_DOC_TITLE)

        assert result is None

    def test_returns_none_on_network_error(self):
        import requests as req_lib

        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            result = gdocs_create("user123", _FAKE_DOC_TITLE)

        assert result is None

    def test_returns_none_when_response_missing_document_id(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = {"title": _FAKE_DOC_TITLE}  # missing documentId

        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=resp,
        ):
            result = gdocs_create("user123", _FAKE_DOC_TITLE)

        assert result is None

    def test_sends_title_in_request_body(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_create_response(),
        ) as mock_post:
            gdocs_create("user123", "Special Title")

        call_kwargs = mock_post.call_args
        json_body = call_kwargs[1].get("json") or call_kwargs.kwargs.get("json", {})
        assert json_body.get("title") == "Special Title"


# ---------------------------------------------------------------------------
# gdocs_edit
# ---------------------------------------------------------------------------


class TestGdocsEdit:
    _sample_requests = [
        {"insertText": {"location": {"index": 1}, "text": "Hello, world!\n"}}
    ]

    def test_returns_true_on_success(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_batch_update_response(),
        ):
            result = gdocs_edit("user123", _FAKE_DOC_ID, self._sample_requests)

        assert result is True

    def test_returns_false_when_no_token(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=None,
        ):
            result = gdocs_edit("user123", _FAKE_DOC_ID, self._sample_requests)

        assert result is False

    def test_returns_false_on_api_error(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_error_response(403),
        ):
            result = gdocs_edit("user123", _FAKE_DOC_ID, self._sample_requests)

        assert result is False

    def test_returns_false_on_network_error(self):
        import requests as req_lib

        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            side_effect=req_lib.exceptions.ConnectionError("timeout"),
        ):
            result = gdocs_edit("user123", _FAKE_DOC_ID, self._sample_requests)

        assert result is False

    def test_accepts_full_url(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_batch_update_response(),
        ) as mock_post:
            result = gdocs_edit("user123", _FAKE_DOC_URL, self._sample_requests)

        assert result is True
        # Verify the called URL contains the extracted doc ID
        called_url = mock_post.call_args[0][0]
        assert _FAKE_DOC_ID in called_url

    def test_sends_requests_in_batch_update_payload(self):
        edit_requests = [
            {"insertText": {"location": {"index": 1}, "text": "A"}},
            {"insertText": {"location": {"index": 2}, "text": "B"}},
        ]
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_batch_update_response(),
        ) as mock_post:
            gdocs_edit("user123", _FAKE_DOC_ID, edit_requests)

        call_kwargs = mock_post.call_args
        json_body = call_kwargs[1].get("json") or call_kwargs.kwargs.get("json", {})
        assert json_body.get("requests") == edit_requests

    def test_batchupdate_url_contains_doc_id_and_suffix(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_batch_update_response(),
        ) as mock_post:
            gdocs_edit("user123", _FAKE_DOC_ID, self._sample_requests)

        called_url = mock_post.call_args[0][0]
        assert _FAKE_DOC_ID in called_url
        assert "batchUpdate" in called_url


# ---------------------------------------------------------------------------
# No token values in logs
# ---------------------------------------------------------------------------


def test_access_token_not_logged_in_create(caplog):
    import logging

    with caplog.at_level(
        logging.DEBUG, logger="integrations.google_workspace.docs_client"
    ):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_create_response(),
        ):
            gdocs_create("user123", _FAKE_DOC_TITLE)

    for record in caplog.records:
        assert _FAKE_ACCESS_TOKEN not in record.getMessage()


def test_access_token_not_logged_in_edit(caplog):
    import logging

    with caplog.at_level(
        logging.DEBUG, logger="integrations.google_workspace.docs_client"
    ):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.post",
            return_value=_batch_update_response(),
        ):
            gdocs_edit(
                "user123",
                _FAKE_DOC_ID,
                [{"insertText": {"location": {"index": 1}, "text": "x"}}],
            )

    for record in caplog.records:
        assert _FAKE_ACCESS_TOKEN not in record.getMessage()
