"""
Tests for src/integrations/fireflies/client.py

Written before the implementation exists (TDD). These exercise the GraphQL
client against a mocked `requests` layer — no real network calls, mirroring
the approach used in tests/unit/test_granola_client.py.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from integrations.fireflies.client import (  # noqa: E402
    ACCOUNT_PRIMARY,
    FirefliesAccountConfig,
    FirefliesAPIError,
    FirefliesAuthError,
    FirefliesNotFoundError,
    FirefliesTranscript,
    FirefliesUnknownAccountError,
    TranscriptListPage,
    build_account_configs_from_env,
    get_transcript,
    iter_all_transcripts,
    iter_all_transcripts_for_account,
    list_transcripts,
)

_ENDPOINT = "https://api.fireflies.ai/graphql"


def _mock_http_response(json_body: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


def _transcript_payload(
    tid: str = "abc123",
    title: str = "Sales call with Acme",
    action_items: str = "- Follow up with Acme on pricing\n- Send proposal by Friday",
) -> dict:
    return {
        "id": tid,
        "title": title,
        "date": "2026-06-01T15:00:00.000Z",
        "duration": 1800,
        "transcript_url": "https://app.fireflies.ai/view/abc123",
        "meeting_link": "https://meet.google.com/xyz",
        "host_email": "alex@example.com",
        "organizer_email": "alex@example.com",
        "participants": ["alex@example.com", "prospect@acme.com"],
        "meeting_attendees": [
            {"name": "Alex", "email": "alex@example.com"},
            {"name": "Prospect Person", "email": "prospect@acme.com"},
        ],
        "summary": {
            "overview": "Discovery call about Acme's CRM needs.",
            "action_items": action_items,
            "keywords": ["CRM", "pricing"],
            "outline": "1. Intro\n2. Needs\n3. Next steps",
            "shorthand_bullet": "- Needs CRM\n- Wants pricing",
            "bullet_gist": "Acme wants a CRM.",
            "gist": "Acme CRM discovery call.",
            "short_summary": "Acme discovery call.",
        },
        "sentences": [
            {
                "index": 0,
                "speaker_name": "Alex",
                "speaker_id": "1",
                "text": "Thanks for joining today.",
                "start_time": 0.0,
                "end_time": 2.5,
            },
            {
                "index": 1,
                "speaker_name": "Prospect Person",
                "speaker_id": "2",
                "text": "Happy to be here.",
                "start_time": 2.5,
                "end_time": 4.0,
            },
        ],
    }


# ---------------------------------------------------------------------------
# list_transcripts
# ---------------------------------------------------------------------------


class TestListTranscripts:
    def test_returns_empty_page(self):
        resp = _mock_http_response({"data": {"transcripts": []}})
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            page = list_transcripts(api_key="ff_test_key")
        assert isinstance(page, TranscriptListPage)
        assert page.transcripts == []
        assert page.has_more is False

    def test_returns_transcripts_summary_only(self):
        payload = {"data": {"transcripts": [
            {"id": "t1", "title": "Call 1", "date": "2026-06-01T10:00:00.000Z"},
            {"id": "t2", "title": "Call 2", "date": "2026-06-02T10:00:00.000Z"},
        ]}}
        resp = _mock_http_response(payload)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            page = list_transcripts(api_key="ff_test_key")
        assert len(page.transcripts) == 2
        assert page.transcripts[0].id == "t1"
        assert page.transcripts[1].title == "Call 2"

    def test_has_more_true_when_page_full(self):
        payload = {"data": {"transcripts": [
            {"id": f"t{i}", "title": f"Call {i}", "date": "2026-06-01T10:00:00.000Z"}
            for i in range(3)
        ]}}
        resp = _mock_http_response(payload)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            page = list_transcripts(api_key="ff_test_key", limit=3)
        assert page.has_more is True

    def test_has_more_false_when_page_partial(self):
        payload = {"data": {"transcripts": [
            {"id": "t1", "title": "Call 1", "date": "2026-06-01T10:00:00.000Z"},
        ]}}
        resp = _mock_http_response(payload)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            page = list_transcripts(api_key="ff_test_key", limit=50)
        assert page.has_more is False

    def test_from_date_passed_as_graphql_variable(self):
        resp = _mock_http_response({"data": {"transcripts": []}})
        captured = {}

        def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
            captured["json"] = json
            return resp

        with patch("integrations.fireflies.client.requests.request", side_effect=fake_request):
            since = datetime(2026, 6, 1, tzinfo=timezone.utc)
            list_transcripts(api_key="ff_test_key", since=since)

        assert captured["json"]["variables"]["fromDate"].startswith("2026-06-01")

    def test_skip_passed_as_graphql_variable(self):
        resp = _mock_http_response({"data": {"transcripts": []}})
        captured = {}

        def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
            captured["json"] = json
            return resp

        with patch("integrations.fireflies.client.requests.request", side_effect=fake_request):
            list_transcripts(api_key="ff_test_key", skip=50)

        assert captured["json"]["variables"]["skip"] == 50

    def test_limit_capped_at_fifty(self):
        """Fireflies caps `limit` at 50 per page — requesting more must be clamped."""
        resp = _mock_http_response({"data": {"transcripts": []}})
        captured = {}

        def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
            captured["json"] = json
            return resp

        with patch("integrations.fireflies.client.requests.request", side_effect=fake_request):
            list_transcripts(api_key="ff_test_key", limit=500)

        assert captured["json"]["variables"]["limit"] == 50

    def test_bearer_header_sent(self):
        resp = _mock_http_response({"data": {"transcripts": []}})
        captured = {}

        def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
            captured["headers"] = headers
            return resp

        with patch("integrations.fireflies.client.requests.request", side_effect=fake_request):
            list_transcripts(api_key="ff_super_secret")

        assert captured["headers"]["Authorization"] == "Bearer ff_super_secret"


# ---------------------------------------------------------------------------
# get_transcript
# ---------------------------------------------------------------------------


class TestGetTranscript:
    def test_returns_full_transcript(self):
        payload = {"data": {"transcript": _transcript_payload()}}
        resp = _mock_http_response(payload)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            t = get_transcript("abc123", api_key="ff_test_key")
        assert isinstance(t, FirefliesTranscript)
        assert t.id == "abc123"
        assert t.title == "Sales call with Acme"
        assert t.summary.action_items.startswith("- Follow up")
        assert len(t.sentences) == 2
        assert t.sentences[0].speaker_name == "Alex"
        assert len(t.meeting_attendees) == 2
        assert t.meeting_attendees[0].email == "alex@example.com"

    def test_account_attribution_defaults_to_primary(self):
        payload = {"data": {"transcript": _transcript_payload()}}
        resp = _mock_http_response(payload)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            t = get_transcript("abc123", api_key="ff_test_key")
        assert t.fireflies_account == ACCOUNT_PRIMARY

    def test_account_attribution_explicit(self):
        payload = {"data": {"transcript": _transcript_payload()}}
        resp = _mock_http_response(payload)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            t = get_transcript("abc123", api_key="ff_test_key", fireflies_account="jake")
        assert t.fireflies_account == "jake"

    def test_missing_transcript_raises_not_found(self):
        payload = {"data": {"transcript": None}}
        resp = _mock_http_response(payload)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            with pytest.raises(FirefliesNotFoundError):
                get_transcript("ghost", api_key="ff_test_key")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_401_raises_auth_error(self):
        resp = _mock_http_response({}, status_code=401)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            with pytest.raises(FirefliesAuthError):
                list_transcripts(api_key="bad_key")

    def test_403_raises_auth_error(self):
        resp = _mock_http_response({}, status_code=403)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            with pytest.raises(FirefliesAuthError):
                list_transcripts(api_key="bad_key")

    def test_graphql_error_array_raises_api_error(self):
        """Fireflies (like most GraphQL APIs) can return HTTP 200 with an `errors` array."""
        resp = _mock_http_response({"errors": [{"message": "Something went wrong"}]})
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            with pytest.raises(FirefliesAPIError):
                list_transcripts(api_key="ff_test_key")

    def test_graphql_auth_error_message_raises_auth_error(self):
        resp = _mock_http_response({"errors": [{"message": "Unauthorized: invalid api key"}]})
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            with pytest.raises(FirefliesAuthError):
                list_transcripts(api_key="bad_key")

    def test_5xx_raises_api_error(self):
        resp = _mock_http_response({}, status_code=500)
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            with pytest.raises(FirefliesAPIError) as exc_info:
                list_transcripts(api_key="ff_test_key")
        assert exc_info.value.status_code == 500

    def test_retries_on_429_then_succeeds(self):
        ok_resp = _mock_http_response({"data": {"transcripts": []}})
        rate_limited_resp = _mock_http_response({}, status_code=429)
        call_count = {"n": 0}

        def fake_request(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                return rate_limited_resp
            return ok_resp

        with patch("integrations.fireflies.client.requests.request", side_effect=fake_request), \
             patch("integrations.fireflies.client.time.sleep"):
            page = list_transcripts(api_key="ff_test_key")

        assert call_count["n"] == 3
        assert page.transcripts == []

    def test_raises_rate_limit_after_max_retries(self):
        resp = _mock_http_response({}, status_code=429)
        with patch("integrations.fireflies.client.requests.request", return_value=resp), \
             patch("integrations.fireflies.client.time.sleep"):
            with pytest.raises(FirefliesAPIError):
                list_transcripts(api_key="ff_test_key")

    def test_missing_api_key_raises_value_error(self):
        import os
        original = os.environ.pop("FIREFLIES_API_KEY", None)
        try:
            with pytest.raises(ValueError, match="FIREFLIES_API_KEY"):
                list_transcripts()
        finally:
            if original is not None:
                os.environ["FIREFLIES_API_KEY"] = original


# ---------------------------------------------------------------------------
# iter_all_transcripts — pagination
# ---------------------------------------------------------------------------


class TestIterAllTranscripts:
    def test_follows_pagination_until_partial_page(self):
        page1 = {"data": {"transcripts": [
            {"id": f"t{i}", "title": "x", "date": "2026-06-01T00:00:00.000Z"} for i in range(2)
        ]}}
        page2 = {"data": {"transcripts": [
            {"id": "t2", "title": "x", "date": "2026-06-01T00:00:00.000Z"},
        ]}}
        responses = [_mock_http_response(page1), _mock_http_response(page2)]

        def fake_request(*args, **kwargs):
            return responses.pop(0)

        with patch("integrations.fireflies.client.requests.request", side_effect=fake_request):
            transcripts = iter_all_transcripts(api_key="ff_test_key", limit=2)

        assert [t.id for t in transcripts] == ["t0", "t1", "t2"]


# ---------------------------------------------------------------------------
# Multi-account support — dynamic FIREFLIES_API_KEY_<NAME> discovery
# ---------------------------------------------------------------------------


class TestBuildAccountConfigsFromEnv:
    def test_empty_env_returns_empty(self):
        assert build_account_configs_from_env({}) == []

    def test_missing_primary_key_returns_empty_even_with_named_keys(self):
        env = {"FIREFLIES_API_KEY_JAKE": "ff_jake"}
        assert build_account_configs_from_env(env) == []

    def test_primary_only(self):
        env = {"FIREFLIES_API_KEY": "ff_primary"}
        configs = build_account_configs_from_env(env)
        assert len(configs) == 1
        assert configs[0] == FirefliesAccountConfig(name=ACCOUNT_PRIMARY, api_key="ff_primary")

    def test_discovers_named_accounts_dynamically(self):
        """
        Regression test for the Granola bug: build_account_configs_from_env()
        must NOT hardcode a fixed set of suffixes (e.g. only '_2'). Any
        FIREFLIES_API_KEY_<NAME> env var must be picked up automatically.
        """
        env = {
            "FIREFLIES_API_KEY": "ff_primary",
            "FIREFLIES_API_KEY_JAKE": "ff_jake",
            "FIREFLIES_API_KEY_BEN": "ff_ben",
            "FIREFLIES_API_KEY_PRIYA": "ff_priya",
        }
        configs = build_account_configs_from_env(env)
        names = {c.name for c in configs}
        assert names == {ACCOUNT_PRIMARY, "jake", "ben", "priya"}

    def test_named_account_keys_correctly_attributed(self):
        env = {
            "FIREFLIES_API_KEY": "ff_primary",
            "FIREFLIES_API_KEY_JAKE": "ff_jake_key",
        }
        configs = build_account_configs_from_env(env)
        by_name = {c.name: c.api_key for c in configs}
        assert by_name["jake"] == "ff_jake_key"
        assert by_name[ACCOUNT_PRIMARY] == "ff_primary"

    def test_primary_account_always_first(self):
        env = {
            "FIREFLIES_API_KEY": "ff_primary",
            "FIREFLIES_API_KEY_ZEBRA": "ff_zebra",
            "FIREFLIES_API_KEY_AARDVARK": "ff_aardvark",
        }
        configs = build_account_configs_from_env(env)
        assert configs[0].name == ACCOUNT_PRIMARY

    def test_named_accounts_deterministically_ordered(self):
        env = {
            "FIREFLIES_API_KEY": "ff_primary",
            "FIREFLIES_API_KEY_ZEBRA": "ff_zebra",
            "FIREFLIES_API_KEY_AARDVARK": "ff_aardvark",
        }
        configs = build_account_configs_from_env(env)
        names = [c.name for c in configs]
        assert names == [ACCOUNT_PRIMARY, "aardvark", "zebra"]

    def test_blank_named_key_is_ignored(self):
        env = {
            "FIREFLIES_API_KEY": "ff_primary",
            "FIREFLIES_API_KEY_JAKE": "   ",
        }
        configs = build_account_configs_from_env(env)
        assert len(configs) == 1

    def test_defaults_to_os_environ_when_none_passed(self, monkeypatch):
        monkeypatch.setenv("FIREFLIES_API_KEY", "ff_from_os_environ")
        configs = build_account_configs_from_env()
        assert configs[0].api_key == "ff_from_os_environ"


class TestIterAllTranscriptsForAccount:
    def test_uses_the_accounts_api_key(self):
        resp = _mock_http_response({"data": {"transcripts": []}})
        captured = {}

        def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
            captured["headers"] = headers
            return resp

        account = FirefliesAccountConfig(name="jake", api_key="ff_jakes_key")
        with patch("integrations.fireflies.client.requests.request", side_effect=fake_request):
            iter_all_transcripts_for_account(account)

        assert captured["headers"]["Authorization"] == "Bearer ff_jakes_key"

    def test_attributes_transcripts_to_the_account(self):
        payload = {"data": {"transcripts": [
            {"id": "t1", "title": "x", "date": "2026-06-01T00:00:00.000Z"},
        ]}}
        resp = _mock_http_response(payload)
        account = FirefliesAccountConfig(name="jake", api_key="ff_jakes_key")
        with patch("integrations.fireflies.client.requests.request", return_value=resp):
            transcripts = iter_all_transcripts_for_account(account)
        assert transcripts[0].fireflies_account == "jake"


class TestFirefliesUnknownAccountError:
    def test_is_a_key_error(self):
        assert issubclass(FirefliesUnknownAccountError, KeyError)

    def test_message_contains_account_name(self):
        err = FirefliesUnknownAccountError("phantom")
        assert "phantom" in str(err)
