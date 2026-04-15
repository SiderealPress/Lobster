"""
Tests for granola_client.py (Slice 1)
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make sure the scheduled-tasks module is importable
_TASKS_DIR = Path(__file__).parent.parent.parent / "scheduled-tasks"
sys.path.insert(0, str(_TASKS_DIR))

from granola_client import (  # noqa: E402
    GranolaAPIError,
    GranolaClient,
    GranolaRateLimitError,
    ListNotesResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(body: dict, status: int = 200):
    """Return a context-manager mock that urllib.request.urlopen can return."""
    raw = json.dumps(body).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = raw
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _http_error(status: int, body: str = ""):
    err = urllib.error.HTTPError(
        url="https://example.com",
        code=status,
        msg=str(status),
        hdrs=None,
        fp=BytesIO(body.encode("utf-8")),
    )
    return err


# ---------------------------------------------------------------------------
# Tests: GranolaClient.list_notes
# ---------------------------------------------------------------------------

class TestListNotes:
    def test_returns_empty_list(self):
        client = GranolaClient(api_key="test_key")
        resp = _mock_response({"notes": [], "hasMore": False, "cursor": None})
        with patch("urllib.request.urlopen", return_value=resp):
            result = client.list_notes()
        assert isinstance(result, ListNotesResult)
        assert result.notes == []
        assert result.has_more is False
        assert result.cursor is None

    def test_returns_notes(self):
        notes_data = [
            {"id": "abc", "title": "Meeting 1", "created_at": "2026-04-10T10:00:00Z"},
            {"id": "def", "title": "Meeting 2", "created_at": "2026-04-11T10:00:00Z"},
        ]
        client = GranolaClient(api_key="test_key")
        resp = _mock_response({"notes": notes_data, "hasMore": False, "cursor": None})
        with patch("urllib.request.urlopen", return_value=resp):
            result = client.list_notes()
        assert len(result.notes) == 2
        assert result.notes[0].id == "abc"
        assert result.notes[1].title == "Meeting 2"

    def test_pagination_cursor(self):
        client = GranolaClient(api_key="test_key")
        resp = _mock_response({
            "notes": [{"id": "x1", "title": "T", "created_at": "2026-01-01T00:00:00Z"}],
            "hasMore": True,
            "cursor": "cursor_abc",
        })
        with patch("urllib.request.urlopen", return_value=resp):
            result = client.list_notes()
        assert result.has_more is True
        assert result.cursor == "cursor_abc"

    def test_passes_created_after_in_url(self):
        client = GranolaClient(api_key="test_key")
        resp = _mock_response({"notes": [], "hasMore": False, "cursor": None})
        captured_url = []

        def fake_urlopen(req, timeout=30):
            captured_url.append(req.full_url)
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.list_notes(created_after="2026-01-01T00:00:00Z")

        assert "created_after=2026-01-01T00%3A00%3A00Z" in captured_url[0]

    def test_passes_cursor_in_url(self):
        client = GranolaClient(api_key="test_key")
        resp = _mock_response({"notes": [], "hasMore": False, "cursor": None})
        captured_url = []

        def fake_urlopen(req, timeout=30):
            captured_url.append(req.full_url)
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.list_notes(cursor="my_cursor")

        assert "cursor=my_cursor" in captured_url[0]


# ---------------------------------------------------------------------------
# Tests: GranolaClient.get_note
# ---------------------------------------------------------------------------

class TestGetNote:
    def test_returns_note_dict(self):
        note_data = {"id": "abc", "title": "Test", "summary": "A meeting"}
        client = GranolaClient(api_key="test_key")
        resp = _mock_response(note_data)
        with patch("urllib.request.urlopen", return_value=resp):
            result = client.get_note("abc")
        assert result["id"] == "abc"
        assert result["title"] == "Test"

    def test_include_transcript_appended_to_url(self):
        client = GranolaClient(api_key="test_key")
        resp = _mock_response({"id": "abc", "transcript": []})
        captured_url = []

        def fake_urlopen(req, timeout=30):
            captured_url.append(req.full_url)
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.get_note("abc", include_transcript=True)

        assert "include=transcript" in captured_url[0]


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_raises_api_error_on_4xx(self):
        client = GranolaClient(api_key="test_key")
        with patch("urllib.request.urlopen", side_effect=_http_error(404, "not found")):
            with pytest.raises(GranolaAPIError) as exc_info:
                client.list_notes()
        assert exc_info.value.status == 404

    def test_raises_api_error_on_5xx(self):
        client = GranolaClient(api_key="test_key")
        with patch("urllib.request.urlopen", side_effect=_http_error(500, "server error")):
            with pytest.raises(GranolaAPIError) as exc_info:
                client.list_notes()
        assert exc_info.value.status == 500

    def test_retries_on_429_then_succeeds(self):
        client = GranolaClient(api_key="test_key", max_retries=3, retry_backoff_base=0.0)
        resp = _mock_response({"notes": [], "hasMore": False, "cursor": None})

        call_count = [0]

        def fake_urlopen(req, timeout=30):
            call_count[0] += 1
            if call_count[0] < 3:
                raise _http_error(429, "rate limited")
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.list_notes()

        assert call_count[0] == 3
        assert result.notes == []

    def test_raises_rate_limit_after_max_retries(self):
        client = GranolaClient(api_key="test_key", max_retries=2, retry_backoff_base=0.0)
        with patch(
            "urllib.request.urlopen",
            side_effect=_http_error(429, "rate limited"),
        ):
            with pytest.raises(GranolaRateLimitError):
                client.list_notes()

    def test_raises_if_no_api_key(self):
        import os
        original = os.environ.pop("GRANOLA_API_KEY", None)
        try:
            with pytest.raises(ValueError, match="GRANOLA_API_KEY"):
                GranolaClient()
        finally:
            if original is not None:
                os.environ["GRANOLA_API_KEY"] = original
