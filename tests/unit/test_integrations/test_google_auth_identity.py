"""
Tests for integrations.google_auth.identity (issue #2153).

Covers:
- fetch_authenticated_email: success, HTTP error, network error, missing
  email field, missing/empty access_token -- never raises.
- check_identity_consistency: pure comparison logic (match / mismatch /
  no_baseline / email_unavailable).
- format_mismatch_warning: only surfaces text for an actual mismatch.
- expected_email_for_chat_id: registry lookup, graceful on missing/invalid
  file.

These are the tests that would fail if the fix in issue #2153 were
reverted: in particular test_check_identity_consistency_detects_mismatch
and test_two_distinct_chat_ids_produce_independent_results directly assert
the cross-account-mixup detection this issue introduces.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from integrations.google_auth.identity import (
    IdentityCheckResult,
    check_expected_identity,
    check_identity_consistency,
    expected_email_for_chat_id,
    fetch_authenticated_email,
    format_mismatch_warning,
)

_MODULE = "integrations.google_auth.identity"


# ---------------------------------------------------------------------------
# fetch_authenticated_email
# ---------------------------------------------------------------------------


def test_fetch_authenticated_email_success():
    mock_resp = MagicMock(ok=True)
    mock_resp.json.return_value = {"email": "account-a@example.com", "email_verified": True}
    with patch(f"{_MODULE}.requests.get", return_value=mock_resp) as mock_get:
        result = fetch_authenticated_email("ya29.fake-token")
    assert result == "account-a@example.com"
    # Bearer auth, not the token in the URL/query string.
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer ya29.fake-token"


def test_fetch_authenticated_email_http_error_returns_none():
    mock_resp = MagicMock(ok=False, status_code=403)
    with patch(f"{_MODULE}.requests.get", return_value=mock_resp):
        assert fetch_authenticated_email("ya29.fake-token") is None


def test_fetch_authenticated_email_network_error_returns_none():
    with patch(
        f"{_MODULE}.requests.get",
        side_effect=requests.exceptions.ConnectionError("no route to host"),
    ):
        assert fetch_authenticated_email("ya29.fake-token") is None


def test_fetch_authenticated_email_missing_email_field_returns_none():
    mock_resp = MagicMock(ok=True)
    mock_resp.json.return_value = {"sub": "12345"}  # no "email" key
    with patch(f"{_MODULE}.requests.get", return_value=mock_resp):
        assert fetch_authenticated_email("ya29.fake-token") is None


def test_fetch_authenticated_email_unparseable_json_returns_none():
    mock_resp = MagicMock(ok=True)
    mock_resp.json.side_effect = ValueError("not json")
    with patch(f"{_MODULE}.requests.get", return_value=mock_resp):
        assert fetch_authenticated_email("ya29.fake-token") is None


def test_fetch_authenticated_email_empty_token_returns_none_without_network_call():
    with patch(f"{_MODULE}.requests.get") as mock_get:
        assert fetch_authenticated_email("") is None
    mock_get.assert_not_called()


def test_fetch_authenticated_email_never_raises_on_unexpected_exception():
    """Defense in depth: even an exception type the code doesn't explicitly
    catch must not propagate out of fetch_authenticated_email (a userinfo
    hiccup must never block a token save)."""
    with patch(f"{_MODULE}.requests.get", side_effect=requests.exceptions.Timeout("slow")):
        assert fetch_authenticated_email("ya29.fake-token") is None


# ---------------------------------------------------------------------------
# check_identity_consistency (pure)
# ---------------------------------------------------------------------------


def test_check_identity_consistency_first_grant_has_no_baseline():
    result = check_identity_consistency(previous_email=None, new_email="account-a@example.com")
    assert result.status == "no_baseline"
    assert result.is_mismatch is False


def test_check_identity_consistency_match():
    result = check_identity_consistency(previous_email="account-a@example.com", new_email="account-a@example.com")
    assert result.status == "match"
    assert result.is_mismatch is False


def test_check_identity_consistency_detects_mismatch():
    """The core regression test for issue #2153: a reconnect that lands on a
    different Google account than the one already on file must be flagged."""
    result = check_identity_consistency(previous_email="account-a@example.com", new_email="account-b@example.com")
    assert result.status == "mismatch"
    assert result.is_mismatch is True
    assert result.baseline_email == "account-a@example.com"
    assert result.new_email == "account-b@example.com"


def test_check_identity_consistency_email_unavailable_when_new_email_missing():
    result = check_identity_consistency(previous_email="account-a@example.com", new_email=None)
    assert result.status == "email_unavailable"
    assert result.is_mismatch is False


def test_check_expected_identity_mismatch():
    """Registry-based check (covers the first-grant case, where
    check_identity_consistency alone has no baseline)."""
    result = check_expected_identity(expected_email="account-a@example.com", new_email="account-b@example.com")
    assert result.status == "mismatch"
    assert result.is_mismatch is True


# ---------------------------------------------------------------------------
# format_mismatch_warning (pure)
# ---------------------------------------------------------------------------


def test_format_mismatch_warning_returns_none_for_match():
    result = IdentityCheckResult("match", "account-a@example.com", "account-a@example.com")
    assert format_mismatch_warning(result, chat_id="1111111111") is None


def test_format_mismatch_warning_returns_none_for_no_baseline():
    result = IdentityCheckResult("no_baseline", None, "account-a@example.com")
    assert format_mismatch_warning(result, chat_id="1111111111") is None


def test_format_mismatch_warning_returns_none_when_email_unavailable():
    result = IdentityCheckResult("email_unavailable", "account-a@example.com", None)
    assert format_mismatch_warning(result, chat_id="1111111111") is None


def test_format_mismatch_warning_surfaces_both_emails_on_mismatch():
    result = IdentityCheckResult("mismatch", "account-a@example.com", "account-b@example.com")
    warning = format_mismatch_warning(result, chat_id="1111111111")
    assert warning is not None
    assert "account-a@example.com" in warning
    assert "account-b@example.com" in warning


# ---------------------------------------------------------------------------
# expected_email_for_chat_id (registry lookup)
# ---------------------------------------------------------------------------


def test_expected_email_for_chat_id_missing_file_returns_none(tmp_path):
    registry = tmp_path / "known-users.json"
    assert expected_email_for_chat_id("1111111111", known_users_path=registry) is None


def test_expected_email_for_chat_id_loads_registry(tmp_path):
    registry = tmp_path / "known-users.json"
    registry.write_text(json.dumps({"1111111111": "account-a@example.com", "2222222222": "account-b@example.com"}))
    assert expected_email_for_chat_id("1111111111", known_users_path=registry) == "account-a@example.com"
    assert expected_email_for_chat_id("2222222222", known_users_path=registry) == "account-b@example.com"


def test_expected_email_for_chat_id_no_entry_returns_none(tmp_path):
    registry = tmp_path / "known-users.json"
    registry.write_text(json.dumps({"2222222222": "account-b@example.com"}))
    assert expected_email_for_chat_id("1111111111", known_users_path=registry) is None


def test_expected_email_for_chat_id_invalid_json_returns_none(tmp_path):
    registry = tmp_path / "known-users.json"
    registry.write_text("{not valid json")
    assert expected_email_for_chat_id("1111111111", known_users_path=registry) is None


def test_expected_email_for_chat_id_non_dict_json_returns_none(tmp_path):
    registry = tmp_path / "known-users.json"
    registry.write_text(json.dumps(["not", "a", "dict"]))
    assert expected_email_for_chat_id("1111111111", known_users_path=registry) is None


# ---------------------------------------------------------------------------
# Two-chat-id isolation (the exact scenario from the production incident)
# ---------------------------------------------------------------------------


def test_two_distinct_chat_ids_produce_independent_results(tmp_path):
    """chat_id A and Person B's chat_id must each be checked against their
    OWN expected email, never cross-contaminating."""
    registry = tmp_path / "known-users.json"
    registry.write_text(json.dumps({"1111111111": "account-a@example.com", "2222222222": "account-b@example.com"}))

    expected_a = expected_email_for_chat_id("1111111111", known_users_path=registry)
    expected_b = expected_email_for_chat_id("2222222222", known_users_path=registry)

    # Simulate the production incident: chat_id A received Person B's token.
    result_a = check_expected_identity(expected_email=expected_a, new_email="account-b@example.com")
    result_b = check_expected_identity(expected_email=expected_b, new_email="account-b@example.com")

    assert result_a.status == "mismatch", "the second account's email under the first chat_id must be flagged"
    assert result_b.status == "match", "the second account's email under its own chat_id must NOT be flagged"
