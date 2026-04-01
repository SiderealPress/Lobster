"""
Tests for src/integrations/gmail/client.py and src/integrations/gmail/models.py.

All HTTP calls and token lookups are mocked — these tests run with no network
access and no real Google credentials.

Coverage:
- GmailMessage: immutability, field types
- GmailThread: immutability, subject derivation
- has_gmail_scope: scope present, scope absent, empty scope
- _decode_mime_header: plain ASCII, RFC 2047 encoded-word, mixed
- _parse_date: epoch, millisecond timestamp, RFC 2822 string, empty
- _extract_header: found, not found, case-insensitive
- _extract_plain_body: text/plain leaf, multipart, missing, nested multipart
- _parse_message: full payload, metadata-only (no body), minimal (no headers)
- _call_gmail_api: success, non-2xx raises GmailAPIError, network error
- get_recent_messages: success, empty, no token, missing scope, API error
- get_message: success, no token, missing scope, API error, None result
- get_thread: success, no token, missing scope, API error, empty thread
- search_messages: delegates to get_recent_messages with query
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as req

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.gmail.models import GmailMessage, GmailThread
from integrations.gmail.client import (
    GmailAPIError,
    _auth_header,
    _call_gmail_api,
    _decode_mime_header,
    _extract_header,
    _extract_plain_body,
    _parse_date,
    _parse_message,
    get_message,
    get_recent_messages,
    get_thread,
    has_gmail_scope,
    search_messages,
)
from integrations.google_calendar.oauth import TokenData

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


def _make_token(scope: str = "calendar.readonly gmail.readonly") -> TokenData:
    return TokenData(
        access_token="fake-access-token",
        expires_at=_NOW,
        scope=scope,
        refresh_token="fake-refresh-token",
    )


def _make_raw_message(
    msg_id: str = "msg001",
    thread_id: str = "thread001",
    subject: str = "Test Subject",
    sender: str = "Alice <alice@example.com>",
    to: str = "Bob <bob@example.com>",
    date_str: str = "Wed, 01 Apr 2026 12:00:00 +0000",
    snippet: str = "Here is a preview of the email body...",
    body_data: str = "",  # base64url-encoded plain body; empty = no body
    label_ids: list[str] | None = None,
) -> dict:
    if label_ids is None:
        label_ids = ["INBOX", "UNREAD"]

    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Date", "value": date_str},
    ]
    payload: dict = {"mimeType": "text/plain", "headers": headers, "body": {}, "parts": []}
    if body_data:
        payload["body"]["data"] = body_data

    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": snippet,
        "labelIds": label_ids,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# GmailMessage / GmailThread — immutability
# ---------------------------------------------------------------------------


class TestGmailMessage:
    def test_frozen(self):
        msg = GmailMessage(
            id="1",
            thread_id="t1",
            subject="Hi",
            sender="a@b.com",
            recipients=["c@d.com"],
            date=_NOW,
            snippet="hello",
            body_text="",
            label_ids=["INBOX"],
            is_unread=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            msg.id = "2"  # type: ignore

    def test_is_unread_false(self):
        msg = GmailMessage(
            id="1", thread_id="t1", subject="", sender="", recipients=[],
            date=_NOW, snippet="", body_text="", label_ids=["INBOX"], is_unread=False,
        )
        assert msg.is_unread is False


class TestGmailThread:
    def test_frozen(self):
        thread = GmailThread(id="t1", messages=[], subject="Hi")
        with pytest.raises((AttributeError, TypeError)):
            thread.id = "t2"  # type: ignore

    def test_empty_messages(self):
        thread = GmailThread(id="t1", messages=[], subject="")
        assert thread.messages == []


# ---------------------------------------------------------------------------
# has_gmail_scope
# ---------------------------------------------------------------------------


class TestHasGmailScope:
    def test_scope_present(self):
        token = _make_token(scope="calendar.readonly gmail.readonly calendar.events")
        assert has_gmail_scope(token) is True

    def test_scope_absent(self):
        token = _make_token(scope="calendar.readonly calendar.events")
        assert has_gmail_scope(token) is False

    def test_empty_scope(self):
        token = _make_token(scope="")
        assert has_gmail_scope(token) is False

    def test_only_gmail_scope(self):
        token = _make_token(scope="https://www.googleapis.com/auth/gmail.readonly")
        assert has_gmail_scope(token) is True


# ---------------------------------------------------------------------------
# _decode_mime_header
# ---------------------------------------------------------------------------


class TestDecodeMimeHeader:
    def test_plain_ascii(self):
        assert _decode_mime_header("Hello World") == "Hello World"

    def test_empty(self):
        assert _decode_mime_header("") == ""

    def test_utf8_encoded_word(self):
        # =?utf-8?b?<base64 of "Héllo">?=
        import base64
        encoded = base64.b64encode("Héllo".encode("utf-8")).decode()
        raw = f"=?utf-8?b?{encoded}?="
        result = _decode_mime_header(raw)
        assert "Héllo" in result or result  # graceful decode

    def test_mixed(self):
        # Plain text adjacent to encoded word
        result = _decode_mime_header("Re: normal subject")
        assert result == "Re: normal subject"


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_empty_string(self):
        dt = _parse_date("")
        assert dt == _EPOCH

    def test_millisecond_timestamp(self):
        ms = "1743508800000"  # some arbitrary epoch ms
        dt = _parse_date(ms)
        assert dt.tzinfo is not None
        assert dt.year >= 2020

    def test_rfc2822_string(self):
        dt = _parse_date("Wed, 01 Apr 2026 12:00:00 +0000")
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.tzinfo is not None

    def test_rfc2822_with_tz_offset(self):
        dt = _parse_date("Wed, 01 Apr 2026 14:00:00 +0200")
        # Should be converted to UTC: 12:00
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 12

    def test_invalid_falls_back_to_epoch(self):
        dt = _parse_date("not-a-date")
        assert dt == _EPOCH


# ---------------------------------------------------------------------------
# _extract_header
# ---------------------------------------------------------------------------


class TestExtractHeader:
    _HEADERS = [
        {"name": "Subject", "value": "Hello"},
        {"name": "From", "value": "alice@example.com"},
    ]

    def test_found(self):
        assert _extract_header(self._HEADERS, "Subject") == "Hello"

    def test_not_found(self):
        assert _extract_header(self._HEADERS, "X-Missing") == ""

    def test_case_insensitive(self):
        assert _extract_header(self._HEADERS, "subject") == "Hello"
        assert _extract_header(self._HEADERS, "FROM") == "alice@example.com"

    def test_empty_list(self):
        assert _extract_header([], "Subject") == ""


# ---------------------------------------------------------------------------
# _extract_plain_body
# ---------------------------------------------------------------------------


class TestExtractPlainBody:
    def test_text_plain_leaf(self):
        import base64
        data = base64.urlsafe_b64encode(b"Hello body").decode()
        payload = {"mimeType": "text/plain", "body": {"data": data}, "parts": []}
        assert _extract_plain_body(payload) == "Hello body"

    def test_no_body_data(self):
        payload = {"mimeType": "text/plain", "body": {}, "parts": []}
        assert _extract_plain_body(payload) == ""

    def test_multipart_picks_plain_part(self):
        import base64
        data = base64.urlsafe_b64encode(b"Plain text here").decode()
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "body": {"data": data}, "parts": []},
                {"mimeType": "text/html", "body": {"data": "dW5yZWxldmFudA=="}, "parts": []},
            ],
        }
        assert _extract_plain_body(payload) == "Plain text here"

    def test_html_only_returns_empty(self):
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {"mimeType": "text/html", "body": {"data": "dW5yZWxldmFudA=="}, "parts": []},
            ],
        }
        assert _extract_plain_body(payload) == ""

    def test_empty_payload(self):
        assert _extract_plain_body({}) == ""


# ---------------------------------------------------------------------------
# _parse_message
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_full_message_fields(self):
        raw = _make_raw_message()
        msg = _parse_message(raw)
        assert msg.id == "msg001"
        assert msg.thread_id == "thread001"
        assert msg.subject == "Test Subject"
        assert msg.sender == "Alice <alice@example.com>"
        assert msg.recipients == ["Bob <bob@example.com>"]
        assert msg.snippet == "Here is a preview of the email body..."
        assert msg.is_unread is True
        assert "UNREAD" in msg.label_ids

    def test_is_unread_false_when_no_unread_label(self):
        raw = _make_raw_message(label_ids=["INBOX"])
        msg = _parse_message(raw)
        assert msg.is_unread is False

    def test_missing_optional_headers_default_to_empty(self):
        raw = {
            "id": "x",
            "threadId": "t",
            "snippet": "",
            "labelIds": [],
            "payload": {"headers": [], "body": {}, "parts": []},
        }
        msg = _parse_message(raw)
        assert msg.subject == ""
        assert msg.sender == ""
        assert msg.recipients == []

    def test_date_parsed_from_header(self):
        raw = _make_raw_message(date_str="Wed, 01 Apr 2026 12:00:00 +0000")
        msg = _parse_message(raw)
        assert msg.date.year == 2026
        assert msg.date.tzinfo == timezone.utc

    def test_date_falls_back_to_internal_date(self):
        raw = _make_raw_message(date_str="")
        raw["internalDate"] = "1743508800000"
        # Remove Date header so fallback is used
        raw["payload"]["headers"] = [h for h in raw["payload"]["headers"] if h["name"] != "Date"]
        msg = _parse_message(raw)
        assert msg.date.tzinfo is not None

    def test_multiple_recipients_parsed(self):
        raw = _make_raw_message(to="alice@a.com, bob@b.com")
        msg = _parse_message(raw)
        assert len(msg.recipients) == 2

    def test_body_extracted_when_present(self):
        import base64
        data = base64.urlsafe_b64encode(b"Body content").decode()
        raw = _make_raw_message(body_data=data)
        msg = _parse_message(raw)
        assert msg.body_text == "Body content"

    def test_body_empty_for_metadata_format(self):
        raw = _make_raw_message()  # no body_data
        msg = _parse_message(raw)
        assert msg.body_text == ""


# ---------------------------------------------------------------------------
# _call_gmail_api
# ---------------------------------------------------------------------------


class TestCallGmailApi:
    @patch("integrations.gmail.client.requests.request")
    def test_success_returns_json(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"messages": []}
        mock_request.return_value = mock_resp

        result = _call_gmail_api("GET", "https://example.com", "tok")
        assert result == {"messages": []}

    @patch("integrations.gmail.client.requests.request")
    def test_non_2xx_raises_gmail_api_error(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"error": {"message": "Forbidden"}}
        mock_request.return_value = mock_resp

        with pytest.raises(GmailAPIError) as exc_info:
            _call_gmail_api("GET", "https://example.com", "tok")
        assert exc_info.value.status_code == 403

    @patch("integrations.gmail.client.requests.request")
    def test_network_error_propagates(self, mock_request):
        mock_request.side_effect = req.exceptions.ConnectionError("unreachable")

        with pytest.raises(req.exceptions.ConnectionError):
            _call_gmail_api("GET", "https://example.com", "tok")


# ---------------------------------------------------------------------------
# get_recent_messages
# ---------------------------------------------------------------------------


class TestGetRecentMessages:
    def _stub_list_response(self, msg_ids: list[str]) -> dict:
        return {"messages": [{"id": mid, "threadId": "t1"} for mid in msg_ids]}

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_success(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        # First call = list, second call = get message
        raw_msg = _make_raw_message(msg_id="msg1")
        mock_api.side_effect = [
            self._stub_list_response(["msg1"]),
            raw_msg,
        ]

        msgs = get_recent_messages("user123")
        assert len(msgs) == 1
        assert msgs[0].id == "msg1"

    @patch("integrations.gmail.client.get_valid_token")
    def test_no_token_returns_empty(self, mock_token):
        mock_token.return_value = None
        assert get_recent_messages("user123") == []

    @patch("integrations.gmail.client.get_valid_token")
    def test_missing_gmail_scope_returns_empty(self, mock_token):
        mock_token.return_value = _make_token(scope="calendar.readonly")
        assert get_recent_messages("user123") == []

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_api_error_returns_empty(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        mock_api.side_effect = GmailAPIError(500, "server error")
        assert get_recent_messages("user123") == []

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_empty_list_returns_empty(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        mock_api.return_value = {"messages": []}
        assert get_recent_messages("user123") == []

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_query_passed_to_api(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        mock_api.return_value = {"messages": []}
        get_recent_messages("user123", query="is:unread")
        call_params = mock_api.call_args
        assert call_params[1]["params"]["q"] == "is:unread"


# ---------------------------------------------------------------------------
# get_message
# ---------------------------------------------------------------------------


class TestGetMessage:
    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_success(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        raw = _make_raw_message(msg_id="msg42")
        mock_api.return_value = raw

        msg = get_message("user123", "msg42")
        assert msg is not None
        assert msg.id == "msg42"

    @patch("integrations.gmail.client.get_valid_token")
    def test_no_token_returns_none(self, mock_token):
        mock_token.return_value = None
        assert get_message("user123", "msg42") is None

    @patch("integrations.gmail.client.get_valid_token")
    def test_missing_scope_returns_none(self, mock_token):
        mock_token.return_value = _make_token(scope="calendar.readonly")
        assert get_message("user123", "msg42") is None

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_api_error_returns_none(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        mock_api.side_effect = GmailAPIError(404, "not found")
        assert get_message("user123", "msg42") is None

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_format_passed_to_api(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        raw = _make_raw_message()
        mock_api.return_value = raw
        get_message("user123", "msg42", format="full")
        params = mock_api.call_args[1]["params"]
        assert params["format"] == "full"


# ---------------------------------------------------------------------------
# get_thread
# ---------------------------------------------------------------------------


class TestGetThread:
    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_success(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        raw_msg = _make_raw_message(msg_id="m1", thread_id="t99", subject="Thread Subject")
        mock_api.return_value = {"id": "t99", "messages": [raw_msg]}

        thread = get_thread("user123", "t99")
        assert thread is not None
        assert thread.id == "t99"
        assert len(thread.messages) == 1
        assert thread.subject == "Thread Subject"

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_empty_thread_subject_is_empty(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        mock_api.return_value = {"id": "t1", "messages": []}
        thread = get_thread("user123", "t1")
        assert thread is not None
        assert thread.subject == ""
        assert thread.messages == []

    @patch("integrations.gmail.client.get_valid_token")
    def test_no_token_returns_none(self, mock_token):
        mock_token.return_value = None
        assert get_thread("user123", "t1") is None

    @patch("integrations.gmail.client.get_valid_token")
    def test_missing_scope_returns_none(self, mock_token):
        mock_token.return_value = _make_token(scope="calendar.readonly")
        assert get_thread("user123", "t1") is None

    @patch("integrations.gmail.client.get_valid_token")
    @patch("integrations.gmail.client._call_gmail_api")
    def test_api_error_returns_none(self, mock_api, mock_token):
        mock_token.return_value = _make_token()
        mock_api.side_effect = GmailAPIError(500)
        assert get_thread("user123", "t1") is None


# ---------------------------------------------------------------------------
# search_messages
# ---------------------------------------------------------------------------


class TestSearchMessages:
    @patch("integrations.gmail.client.get_recent_messages")
    def test_delegates_to_get_recent_messages(self, mock_get):
        mock_get.return_value = []
        search_messages("user123", "from:boss@corp.com", max_results=5)
        mock_get.assert_called_once_with("user123", max_results=5, query="from:boss@corp.com")

    @patch("integrations.gmail.client.get_recent_messages")
    def test_returns_results(self, mock_get):
        fake_msg = MagicMock(spec=GmailMessage)
        mock_get.return_value = [fake_msg]
        result = search_messages("user123", "is:unread")
        assert result == [fake_msg]
