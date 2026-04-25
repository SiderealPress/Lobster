"""
Unit tests for scheduled-tasks/gmail-poll.py.

Tests cover the pure helper functions that drive new-mail detection and
inbox message construction. All I/O (Gmail API, disk, token store) is
absent from pure function tests — no mocking required.

Named after behaviors, not mechanisms:
  - test_parse_sender_with_display_name
  - test_should_skip_self_sent_messages
  - test_should_skip_promotional_category_messages
  - test_extract_body_prefers_text_plain
  ... etc.
"""

from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test.
#
# gmail-poll.py is a script (not a package module), so we load it via
# importlib with a path-based spec.  The import is done at module level so
# the test module fails fast if the script is broken.
# ---------------------------------------------------------------------------

SCRIPT_PATH = Path(__file__).parent.parent.parent.parent / "scheduled-tasks" / "gmail-poll.py"

# Stub out the token_store import that gmail-poll.py does at the top level,
# so the test file can be imported without the full Lobster src on the path.
_token_store_stub = MagicMock()
_token_store_stub.get_valid_token = MagicMock(return_value=None)

with patch.dict(
    "sys.modules",
    {
        "integrations": MagicMock(),
        "integrations.gmail": MagicMock(),
        "integrations.gmail.token_store": _token_store_stub,
    },
):
    spec = importlib.util.spec_from_file_location("gmail_poll", SCRIPT_PATH)
    _gp: ModuleType = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(_gp)  # type: ignore[union-attr]

# Pull names into local scope for convenience.
parse_sender = _gp.parse_sender
get_header = _gp.get_header
decode_base64url = _gp.decode_base64url
extract_body = _gp.extract_body
should_skip_message = _gp.should_skip_message
build_inbox_message = _gp.build_inbox_message
SKIP_LABEL_IDS = _gp.SKIP_LABEL_IDS


# ---------------------------------------------------------------------------
# parse_sender
# ---------------------------------------------------------------------------


class TestParseSender:
    def test_parses_display_name_and_email(self) -> None:
        name, email = parse_sender("Jane Doe <jane@example.com>")
        assert name == "Jane Doe"
        assert email == "jane@example.com"

    def test_bare_email_returns_none_name(self) -> None:
        name, email = parse_sender("jane@example.com")
        assert name is None
        assert email == "jane@example.com"

    def test_strips_surrounding_whitespace(self) -> None:
        name, email = parse_sender("  Jane  <jane@example.com>  ")
        assert email == "jane@example.com"

    def test_strips_quotes_from_display_name(self) -> None:
        name, email = parse_sender('"Acme Corp" <hello@acme.com>')
        assert name == "Acme Corp"
        assert email == "hello@acme.com"

    def test_empty_display_name_becomes_none(self) -> None:
        name, email = parse_sender(" <jane@example.com>")
        assert name is None
        assert email == "jane@example.com"


# ---------------------------------------------------------------------------
# get_header
# ---------------------------------------------------------------------------


class TestGetHeader:
    def _headers(self) -> list[dict]:
        return [
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "Subject", "value": "Hello there"},
            {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
        ]

    def test_returns_matching_header_value(self) -> None:
        assert get_header(self._headers(), "Subject") == "Hello there"

    def test_is_case_insensitive(self) -> None:
        assert get_header(self._headers(), "from") == "Alice <alice@example.com>"
        assert get_header(self._headers(), "FROM") == "Alice <alice@example.com>"

    def test_returns_none_when_header_absent(self) -> None:
        assert get_header(self._headers(), "X-Custom") is None

    def test_empty_header_list_returns_none(self) -> None:
        assert get_header([], "Subject") is None

    def test_returns_first_match_when_duplicate_headers_present(self) -> None:
        headers = [
            {"name": "X-Dup", "value": "first"},
            {"name": "X-Dup", "value": "second"},
        ]
        assert get_header(headers, "X-Dup") == "first"


# ---------------------------------------------------------------------------
# decode_base64url
# ---------------------------------------------------------------------------


class TestDecodeBase64url:
    def test_decodes_standard_base64url_string(self) -> None:
        # "Hello, World!" encoded as base64url (without padding)
        encoded = base64.urlsafe_b64encode(b"Hello, World!").rstrip(b"=").decode()
        assert decode_base64url(encoded) == "Hello, World!"

    def test_handles_missing_padding(self) -> None:
        # base64url without padding — decode_base64url must add it
        raw = "plain text body"
        padded = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
        assert decode_base64url(padded) == raw

    def test_handles_utf8_content(self) -> None:
        content = "Bonjour — café"
        encoded = base64.urlsafe_b64encode(content.encode("utf-8")).rstrip(b"=").decode()
        assert decode_base64url(encoded) == content


# ---------------------------------------------------------------------------
# extract_body
# ---------------------------------------------------------------------------

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


class TestExtractBody:
    def test_extracts_text_plain_body_directly(self) -> None:
        payload = {"mimeType": "text/plain", "body": {"data": _b64("Hello plain text")}}
        assert extract_body(payload) == "Hello plain text"

    def test_returns_empty_string_when_no_data(self) -> None:
        payload = {"mimeType": "text/plain", "body": {}}
        assert extract_body(payload) == ""

    def test_prefers_text_plain_among_multipart_parts(self) -> None:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("plain version")}},
                {"mimeType": "text/html", "body": {"data": _b64("<b>html version</b>")}},
            ],
        }
        assert extract_body(payload) == "plain version"

    def test_falls_back_through_parts_when_no_text_plain(self) -> None:
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<b>html version</b>")}},
            ],
        }
        result = extract_body(payload)
        assert "<b>html version</b>" in result

    def test_recurses_into_nested_multipart(self) -> None:
        inner_text = "deep nested plain"
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64(inner_text)}},
                    ],
                }
            ],
        }
        assert extract_body(payload) == inner_text

    def test_returns_empty_string_for_empty_payload(self) -> None:
        assert extract_body({}) == ""


# ---------------------------------------------------------------------------
# should_skip_message
# ---------------------------------------------------------------------------

ACCOUNT_EMAIL = "LOBSTER_GMAIL_ADDRESS"

# SKIP_LABEL_IDS is a frozenset defined in the module.
PROMO_LABEL = next(iter(SKIP_LABEL_IDS))  # any one skip label


class TestShouldSkipMessage:
    def test_skips_self_sent_messages(self) -> None:
        assert should_skip_message([], ACCOUNT_EMAIL, ACCOUNT_EMAIL) is True

    def test_skips_self_sent_case_insensitive(self) -> None:
        assert should_skip_message([], ACCOUNT_EMAIL.upper(), ACCOUNT_EMAIL) is True

    def test_skips_promotional_category(self) -> None:
        assert should_skip_message(["CATEGORY_PROMOTIONS"], "other@example.com", ACCOUNT_EMAIL) is True

    def test_skips_social_category(self) -> None:
        assert should_skip_message(["CATEGORY_SOCIAL"], "other@example.com", ACCOUNT_EMAIL) is True

    def test_skips_updates_category(self) -> None:
        assert should_skip_message(["CATEGORY_UPDATES"], "other@example.com", ACCOUNT_EMAIL) is True

    def test_does_not_skip_external_inbox_message(self) -> None:
        assert should_skip_message(["INBOX"], "investor@example.com", ACCOUNT_EMAIL) is False

    def test_does_not_skip_when_labels_empty(self) -> None:
        assert should_skip_message([], "investor@example.com", ACCOUNT_EMAIL) is False

    def test_skip_labels_override_inbox_label(self) -> None:
        # INBOX + CATEGORY_PROMOTIONS -> still skip
        assert should_skip_message(
            ["INBOX", "CATEGORY_PROMOTIONS"], "investor@example.com", ACCOUNT_EMAIL
        ) is True


# ---------------------------------------------------------------------------
# build_inbox_message
# ---------------------------------------------------------------------------


class TestBuildInboxMessage:
    def _call(
        self,
        *,
        gmail_message_id: str = "msg123",
        thread_id: str = "thread456",
        subject: Optional[str] = "Investment Inquiry",
        from_name: Optional[str] = "Bob Investor",
        from_email: str = "bob@investor.com",
        to_header: Optional[str] = "LOBSTER_GMAIL_ADDRESS",
        body_text: str = "Hi, I am interested in investing.",
        received_at: str = "2024-01-01T00:00:00+00:00",
        account_email: str = ACCOUNT_EMAIL,
    ) -> dict:
        return build_inbox_message(
            gmail_message_id=gmail_message_id,
            thread_id=thread_id,
            subject=subject,
            from_name=from_name,
            from_email=from_email,
            to_header=to_header,
            body_text=body_text,
            received_at=received_at,
            account_email=account_email,
        )

    def test_message_id_has_gmail_prefix(self) -> None:
        msg = self._call(gmail_message_id="abc123")
        assert msg["id"] == "gmail-abc123"

    def test_source_is_gmail(self) -> None:
        assert self._call()["source"] == "gmail"

    def test_text_contains_sender_display_name(self) -> None:
        msg = self._call(from_name="Bob Investor", from_email="bob@investor.com")
        assert "Bob Investor" in msg["text"]

    def test_text_contains_subject(self) -> None:
        msg = self._call(subject="Q1 Investment Opportunity")
        assert "Q1 Investment Opportunity" in msg["text"]

    def test_text_contains_body(self) -> None:
        msg = self._call(body_text="Here is my investment proposal.")
        assert "Here is my investment proposal." in msg["text"]

    def test_metadata_contains_gmail_ids(self) -> None:
        msg = self._call(gmail_message_id="msg999", thread_id="thr999")
        assert msg["metadata"]["gmail_message_id"] == "msg999"
        assert msg["metadata"]["gmail_thread_id"] == "thr999"

    def test_metadata_subject_fallback_for_none(self) -> None:
        msg = self._call(subject=None)
        assert msg["metadata"]["subject"] == "(no subject)"

    def test_uses_email_as_display_name_when_no_from_name(self) -> None:
        msg = self._call(from_name=None, from_email="anon@example.com")
        assert "anon@example.com" in msg["text"]

    def test_chat_id_is_account_email(self) -> None:
        msg = self._call(account_email=ACCOUNT_EMAIL)
        assert msg["chat_id"] == ACCOUNT_EMAIL

    def test_timestamp_is_received_at(self) -> None:
        ts = "2024-06-15T12:00:00+00:00"
        msg = self._call(received_at=ts)
        assert msg["timestamp"] == ts
