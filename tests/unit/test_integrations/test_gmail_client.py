"""
Unit tests for src/integrations/gmail/client.py (BIS-256).

All HTTP and token I/O is mocked.  Tests verify the public API contract:
- get_recent_emails returns EmailMessage list on success, [] on failure
- search_emails returns EmailMessage list on success, [] on failure
- Pure helpers (_parse_header, _parse_date_header, _parse_message) are tested
  independently to verify correctness without network calls.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.gmail import client as gc
from integrations.gmail.client import (
    EmailMessage,
    GmailAPIError,
    _auth_header,
    _parse_date_header,
    _parse_header,
    _parse_message,
    get_recent_emails,
    search_emails,
)
from integrations.google_calendar.oauth import TokenData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FUTURE = datetime.now(tz=timezone.utc) + timedelta(hours=2)

_VALID_TOKEN = TokenData(
    access_token="test-access-token",
    expires_at=_FUTURE,
    scope="https://mail.google.com/",
    refresh_token="refresh-tok",
)

_SAMPLE_HEADERS = [
    {"name": "Subject", "value": "Hello World"},
    {"name": "From", "value": "Alice <alice@example.com>"},
    {"name": "Date", "value": "Wed, 1 Apr 2026 14:30:00 +0000"},
]

_SAMPLE_RAW_MESSAGE = {
    "id": "msg1",
    "threadId": "thread1",
    "snippet": "Hello there, this is a snippet",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "headers": _SAMPLE_HEADERS,
    },
}

_SAMPLE_LIST_RESPONSE = {
    "messages": [{"id": "msg1"}, {"id": "msg2"}],
}


# ---------------------------------------------------------------------------
# _parse_header — pure function
# ---------------------------------------------------------------------------


class TestParseHeader:
    def test_finds_subject(self):
        val = _parse_header(_SAMPLE_HEADERS, "Subject")
        assert val == "Hello World"

    def test_case_insensitive(self):
        val = _parse_header(_SAMPLE_HEADERS, "subject")
        assert val == "Hello World"

    def test_returns_empty_when_absent(self):
        val = _parse_header(_SAMPLE_HEADERS, "X-Missing")
        assert val == ""

    def test_empty_headers_list(self):
        val = _parse_header([], "Subject")
        assert val == ""


# ---------------------------------------------------------------------------
# _parse_date_header — pure function
# ---------------------------------------------------------------------------


class TestParseDateHeader:
    def test_parses_rfc2822_date(self):
        dt = _parse_date_header("Wed, 1 Apr 2026 14:30:00 +0000")
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 1
        assert dt.tzinfo is not None

    def test_returns_utc_datetime_on_empty(self):
        dt = _parse_date_header("")
        assert dt.tzinfo is not None

    def test_returns_utc_datetime_on_garbage(self):
        dt = _parse_date_header("not a date at all")
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# _parse_message — pure function
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_parses_subject(self):
        msg = _parse_message(_SAMPLE_RAW_MESSAGE)
        assert msg.subject == "Hello World"

    def test_parses_sender(self):
        msg = _parse_message(_SAMPLE_RAW_MESSAGE)
        assert "alice@example.com" in msg.sender

    def test_parses_snippet(self):
        msg = _parse_message(_SAMPLE_RAW_MESSAGE)
        assert msg.snippet == "Hello there, this is a snippet"

    def test_parses_labels(self):
        msg = _parse_message(_SAMPLE_RAW_MESSAGE)
        assert "INBOX" in msg.labels
        assert "UNREAD" in msg.labels

    def test_parses_id_and_thread_id(self):
        msg = _parse_message(_SAMPLE_RAW_MESSAGE)
        assert msg.id == "msg1"
        assert msg.thread_id == "thread1"

    def test_missing_optional_fields_default_to_empty(self):
        msg = _parse_message({"id": "x", "threadId": "t"})
        assert msg.subject == ""
        assert msg.sender == ""
        assert msg.snippet == ""
        assert msg.labels == ()

    def test_is_frozen_dataclass(self):
        msg = _parse_message(_SAMPLE_RAW_MESSAGE)
        with pytest.raises((AttributeError, TypeError)):
            msg.subject = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _auth_header — pure function
# ---------------------------------------------------------------------------


class TestAuthHeader:
    def test_returns_bearer_header(self):
        h = _auth_header("my-token")
        assert h == {"Authorization": "Bearer my-token"}


# ---------------------------------------------------------------------------
# GmailAPIError
# ---------------------------------------------------------------------------


class TestGmailAPIError:
    def test_contains_status_code(self):
        err = GmailAPIError(403, "Forbidden")
        assert "403" in str(err)
        assert "Forbidden" in str(err)

    def test_no_summary_still_valid(self):
        err = GmailAPIError(500)
        assert "500" in str(err)


# ---------------------------------------------------------------------------
# get_recent_emails
# ---------------------------------------------------------------------------


def _mock_get_token_valid(user_id, token_dir=None):
    return _VALID_TOKEN


def _mock_get_token_none(user_id, token_dir=None):
    return None


class TestGetRecentEmails:
    def test_returns_empty_when_no_token(self, tmp_path):
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_none,
        ):
            result = get_recent_emails("999", token_dir=tmp_path)
        assert result == []

    def test_returns_messages_on_success(self, tmp_path):
        def mock_api_call(method, url, token, **kwargs):
            if "messages" in url and "/" not in url.split("messages")[-1].lstrip("/"):
                # List call
                return _SAMPLE_LIST_RESPONSE
            else:
                # Individual message fetch
                msg_id = url.rstrip("/").split("/")[-1]
                raw = dict(_SAMPLE_RAW_MESSAGE)
                raw["id"] = msg_id
                return raw

        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            side_effect=mock_api_call,
        ):
            result = get_recent_emails("123", token_dir=tmp_path)

        assert len(result) == 2
        assert all(isinstance(m, EmailMessage) for m in result)

    def test_returns_empty_on_api_error(self, tmp_path):
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            side_effect=GmailAPIError(403, "Forbidden"),
        ):
            result = get_recent_emails("123", token_dir=tmp_path)
        assert result == []

    def test_returns_empty_on_network_error(self, tmp_path):
        import requests as req_lib

        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            side_effect=req_lib.exceptions.ConnectionError("refused"),
        ):
            result = get_recent_emails("123", token_dir=tmp_path)
        assert result == []

    def test_returns_empty_when_inbox_is_empty(self, tmp_path):
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            return_value={"messages": []},
        ):
            result = get_recent_emails("123", token_dir=tmp_path)
        assert result == []

    def test_skips_failed_individual_message(self, tmp_path):
        """If one message fetch fails, others should still be returned."""
        call_count = [0]

        def selective_fail(method, url, token, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # List call succeeds
                return {"messages": [{"id": "good"}, {"id": "bad"}]}
            elif "good" in url:
                return dict(_SAMPLE_RAW_MESSAGE, id="good")
            else:
                raise GmailAPIError(500, "Server error")

        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            side_effect=selective_fail,
        ):
            result = get_recent_emails("123", token_dir=tmp_path)

        # Should get 1 successful message even though 1 failed
        assert len(result) == 1
        assert result[0].id == "good"

    def test_respects_max_results_param(self, tmp_path):
        """max_results must be forwarded to the list API call."""
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            return_value={"messages": []},
        ) as mock_call:
            get_recent_emails("123", max_results=3, token_dir=tmp_path)

        params = mock_call.call_args.kwargs.get("params", {})
        assert params.get("maxResults") == 3


# ---------------------------------------------------------------------------
# search_emails
# ---------------------------------------------------------------------------


class TestSearchEmails:
    def test_returns_empty_when_no_token(self, tmp_path):
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_none,
        ):
            result = search_emails("999", "from:boss@example.com", token_dir=tmp_path)
        assert result == []

    def test_returns_messages_on_success(self, tmp_path):
        def mock_api_call(method, url, token, **kwargs):
            if "messages" in url and kwargs.get("params", {}).get("q"):
                return {"messages": [{"id": "m1"}]}
            else:
                return dict(_SAMPLE_RAW_MESSAGE, id="m1")

        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            side_effect=mock_api_call,
        ):
            result = search_emails("123", "from:alice@example.com", token_dir=tmp_path)

        assert len(result) == 1
        assert isinstance(result[0], EmailMessage)

    def test_forwards_query_param(self, tmp_path):
        """The search query must be passed as the 'q' parameter."""
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            return_value={"messages": []},
        ) as mock_call:
            search_emails("123", "subject:invoice", token_dir=tmp_path)

        params = mock_call.call_args.kwargs.get("params", {})
        assert params.get("q") == "subject:invoice"

    def test_returns_empty_when_no_results(self, tmp_path):
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            return_value={"messages": []},
        ):
            result = search_emails("123", "from:nobody@x.com", token_dir=tmp_path)
        assert result == []

    def test_returns_empty_on_api_error(self, tmp_path):
        with patch(
            "integrations.gmail.client.get_valid_token",
            side_effect=_mock_get_token_valid,
        ), patch(
            "integrations.gmail.client._call_gmail_api",
            side_effect=GmailAPIError(401, "Unauthorized"),
        ):
            result = search_emails("123", "anything", token_dir=tmp_path)
        assert result == []
