"""
Tests for gdocs_read in src/integrations/google_workspace/docs_client.py (Slice 2).

Covers:
- gdocs_read: returns plain text from a valid API response
- gdocs_read: returns None when no valid token
- gdocs_read: returns None on 401 (expired/revoked token)
- gdocs_read: returns None on 404 (doc not found)
- gdocs_read: returns None on network error
- _doc_id_from_url: extracts ID from full URL
- _doc_id_from_url: returns raw string if not a URL
- _extract_text_from_doc: extracts text from paragraphs
- _extract_text_from_doc: handles empty document
- _extract_text_from_doc: handles table cells
- No token values appear in logs
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_workspace.docs_client import (
    _doc_id_from_url,
    _extract_text_from_doc,
    gdocs_read,
)
from integrations.google_calendar.oauth import TokenData
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_ACCESS_TOKEN = "ya29.fake-workspace-token"
_FAKE_DOC_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
_FAKE_DOC_URL = f"https://docs.google.com/document/d/{_FAKE_DOC_ID}/edit"


def _valid_token() -> TokenData:
    return TokenData(
        access_token=_FAKE_ACCESS_TOKEN,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        scope="https://www.googleapis.com/auth/documents",
        refresh_token="test-refresh",
    )


def _mock_doc_response(text: str = "Hello, World!") -> dict:
    """Build a minimal Docs API response body."""
    return {
        "documentId": _FAKE_DOC_ID,
        "title": "Test Document",
        "body": {
            "content": [
                {
                    "paragraph": {
                        "elements": [
                            {"textRun": {"content": text}}
                        ]
                    }
                }
            ]
        }
    }


def _http_response(status_code: int = 200, json_data: dict = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.json.return_value = json_data or {}
    return resp


# ---------------------------------------------------------------------------
# _doc_id_from_url — pure function tests
# ---------------------------------------------------------------------------


class TestDocIdFromUrl:
    def test_extracts_id_from_full_edit_url(self):
        result = _doc_id_from_url(_FAKE_DOC_URL)
        assert result == _FAKE_DOC_ID

    def test_extracts_id_from_view_url(self):
        url = f"https://docs.google.com/document/d/{_FAKE_DOC_ID}/view"
        result = _doc_id_from_url(url)
        assert result == _FAKE_DOC_ID

    def test_returns_raw_id_when_no_url_match(self):
        result = _doc_id_from_url(_FAKE_DOC_ID)
        assert result == _FAKE_DOC_ID

    def test_strips_whitespace_from_raw_id(self):
        result = _doc_id_from_url(f"  {_FAKE_DOC_ID}  ")
        assert result == _FAKE_DOC_ID


# ---------------------------------------------------------------------------
# _extract_text_from_doc — pure function tests
# ---------------------------------------------------------------------------


class TestExtractTextFromDoc:
    def test_extracts_paragraph_text(self):
        doc = _mock_doc_response("Hello, World!\n")
        result = _extract_text_from_doc(doc)
        assert "Hello, World!" in result

    def test_handles_empty_document(self):
        doc = {"body": {"content": []}}
        result = _extract_text_from_doc(doc)
        assert result == ""

    def test_handles_missing_body(self):
        result = _extract_text_from_doc({})
        assert result == ""

    def test_concatenates_multiple_paragraphs(self):
        doc = {
            "body": {
                "content": [
                    {"paragraph": {"elements": [{"textRun": {"content": "Line 1\n"}}]}},
                    {"paragraph": {"elements": [{"textRun": {"content": "Line 2\n"}}]}},
                ]
            }
        }
        result = _extract_text_from_doc(doc)
        assert "Line 1" in result
        assert "Line 2" in result

    def test_handles_table_cells(self):
        doc = {
            "body": {
                "content": [
                    {
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {
                                            "content": [
                                                {
                                                    "paragraph": {
                                                        "elements": [
                                                            {"textRun": {"content": "Cell text"}}
                                                        ]
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ]
            }
        }
        result = _extract_text_from_doc(doc)
        assert "Cell text" in result

    def test_skips_empty_text_runs(self):
        doc = {
            "body": {
                "content": [
                    {"paragraph": {"elements": [{"textRun": {"content": ""}}]}}
                ]
            }
        }
        result = _extract_text_from_doc(doc)
        assert result == ""


# ---------------------------------------------------------------------------
# gdocs_read — integration tests (mocked)
# ---------------------------------------------------------------------------


class TestGdocsRead:
    def test_returns_text_on_success(self):
        doc_json = _mock_doc_response("Document content here.\n")
        http_resp = _http_response(200, doc_json)

        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.get",
            return_value=http_resp,
        ):
            result = gdocs_read("user123", _FAKE_DOC_ID)

        assert result is not None
        assert "Document content here." in result

    def test_returns_none_when_no_token(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=None,
        ):
            result = gdocs_read("user123", _FAKE_DOC_ID)

        assert result is None

    def test_returns_none_on_401(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.get",
            return_value=_http_response(401),
        ):
            result = gdocs_read("user123", _FAKE_DOC_ID)

        assert result is None

    def test_returns_none_on_404(self):
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.get",
            return_value=_http_response(404),
        ):
            result = gdocs_read("user123", "nonexistent-doc-id")

        assert result is None

    def test_returns_none_on_network_error(self):
        import requests as req_lib
        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.get",
            side_effect=req_lib.exceptions.ConnectionError("connection refused"),
        ):
            result = gdocs_read("user123", _FAKE_DOC_ID)

        assert result is None

    def test_accepts_full_url(self):
        doc_json = _mock_doc_response("URL-based read.\n")
        http_resp = _http_response(200, doc_json)

        with patch(
            "integrations.google_workspace.docs_client.get_valid_token",
            return_value=_valid_token(),
        ), patch(
            "integrations.google_workspace.docs_client.requests.get",
            return_value=http_resp,
        ) as mock_get:
            gdocs_read("user123", _FAKE_DOC_URL)

        # Verify the call used the extracted doc ID, not the full URL
        called_url = mock_get.call_args[0][0]
        assert _FAKE_DOC_ID in called_url
        assert "docs.google.com" not in called_url  # full URL not passed to API

    def test_access_token_not_in_logs(self, caplog):
        import logging
        doc_json = _mock_doc_response("Test\n")
        http_resp = _http_response(200, doc_json)

        with caplog.at_level(logging.DEBUG, logger="integrations.google_workspace.docs_client"):
            with patch(
                "integrations.google_workspace.docs_client.get_valid_token",
                return_value=_valid_token(),
            ), patch(
                "integrations.google_workspace.docs_client.requests.get",
                return_value=http_resp,
            ):
                gdocs_read("user123", _FAKE_DOC_ID)

        for record in caplog.records:
            assert _FAKE_ACCESS_TOKEN not in record.getMessage()
