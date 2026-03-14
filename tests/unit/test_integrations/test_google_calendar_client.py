"""
Tests for src/integrations/google_calendar/client.py — Phase 3.

All HTTP calls and token lookups are mocked so these tests run without any
network access or real Google credentials.

Coverage:
- CalendarEvent: immutability, field defaults
- CalendarAPIError: status_code attribute, message format, no tokens in message
- _parse_datetime: RFC 3339 formats, all-day dates, naive datetimes, non-UTC offsets
- _parse_event: full event, missing optional fields, all-day event, missing dates
- _build_event_body: required fields, optional fields omitted when empty
- _auth_header: correct format, pure function
- _call_calendar_api: success path, non-2xx raises CalendarAPIError, network errors
- get_upcoming_events: success, empty result, auth failure (no token), API error
- create_event: success, default end time (+1h), auth failure, API error
- gcal_add_link re-export: importable from client module
- default credentials loading: get_upcoming_events / create_event pass
  credentials=None through to get_valid_token
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as req

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from integrations.google_calendar.config import (
    DEFAULT_SCOPES,
    GoogleOAuthCredentials,
)
from integrations.google_calendar.client import (
    CalendarAPIError,
    CalendarEvent,
    _auth_header,
    _build_event_body,
    _call_calendar_api,
    _parse_datetime,
    _parse_event,
    create_event,
    gcal_add_link,
    get_upcoming_events,
)
from integrations.google_calendar.oauth import TokenData

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_FAKE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
_FAKE_CLIENT_SECRET = "fake-client-secret"
_FAKE_REDIRECT_URI = "https://myownlobster.ai/auth/google/callback"

_FAKE_CREDENTIALS = GoogleOAuthCredentials(
    client_id=_FAKE_CLIENT_ID,
    client_secret=_FAKE_CLIENT_SECRET,
    scopes=DEFAULT_SCOPES,
    redirect_uri=_FAKE_REDIRECT_URI,
)

_FAKE_USER_ID = "1234567890"
_FAKE_ACCESS_TOKEN = "ya29.fake-access-token"

_NOW = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)
_START = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
_END = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)

_FAKE_TOKEN = TokenData(
    access_token=_FAKE_ACCESS_TOKEN,
    expires_at=_NOW + timedelta(hours=1),
    scope="https://www.googleapis.com/auth/calendar.readonly "
          "https://www.googleapis.com/auth/calendar.events",
    refresh_token="1//fake-refresh-token",
)

_FAKE_EVENT_DICT = {
    "id": "event-abc-123",
    "summary": "Team standup",
    "start": {"dateTime": "2026-03-07T15:00:00Z"},
    "end": {"dateTime": "2026-03-07T16:00:00Z"},
    "description": "Daily sync",
    "location": "Conference room A",
    "htmlLink": "https://calendar.google.com/event?eid=abc123",
}

_FAKE_EVENT_MINIMAL = {
    "id": "event-min-456",
    "summary": "Minimal event",
    "start": {"dateTime": "2026-03-07T17:00:00Z"},
    "end": {"dateTime": "2026-03-07T18:00:00Z"},
}

_FAKE_ALL_DAY_EVENT = {
    "id": "event-allday-789",
    "summary": "All day event",
    "start": {"date": "2026-03-08"},
    "end": {"date": "2026-03-09"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response-like object."""
    mock = MagicMock()
    mock.ok = 200 <= status_code < 300
    mock.status_code = status_code
    mock.json.return_value = json_data
    return mock


# ---------------------------------------------------------------------------
# CalendarEvent
# ---------------------------------------------------------------------------


class TestCalendarEvent:
    def test_is_frozen_dataclass(self) -> None:
        event = CalendarEvent(id="e1", title="Meeting", start=_START, end=_END)
        with pytest.raises(Exception):
            event.title = "Changed"  # type: ignore[misc]

    def test_required_fields(self) -> None:
        event = CalendarEvent(id="e1", title="Meeting", start=_START, end=_END)
        assert event.id == "e1"
        assert event.title == "Meeting"
        assert event.start == _START
        assert event.end == _END

    def test_description_defaults_to_empty_string(self) -> None:
        event = CalendarEvent(id="e1", title="Meeting", start=_START, end=_END)
        assert event.description == ""

    def test_location_defaults_to_empty_string(self) -> None:
        event = CalendarEvent(id="e1", title="Meeting", start=_START, end=_END)
        assert event.location == ""

    def test_url_defaults_to_none(self) -> None:
        event = CalendarEvent(id="e1", title="Meeting", start=_START, end=_END)
        assert event.url is None

    def test_all_fields_set(self) -> None:
        event = CalendarEvent(
            id="e1",
            title="Meeting",
            start=_START,
            end=_END,
            description="Catch-up",
            location="Room 42",
            url="https://calendar.google.com/event?eid=e1",
        )
        assert event.description == "Catch-up"
        assert event.location == "Room 42"
        assert event.url == "https://calendar.google.com/event?eid=e1"


# ---------------------------------------------------------------------------
# CalendarAPIError
# ---------------------------------------------------------------------------


class TestCalendarAPIError:
    def test_is_runtime_error(self) -> None:
        assert issubclass(CalendarAPIError, RuntimeError)

    def test_status_code_attribute(self) -> None:
        exc = CalendarAPIError(status_code=403)
        assert exc.status_code == 403

    def test_message_includes_status_code(self) -> None:
        exc = CalendarAPIError(status_code=404)
        assert "404" in str(exc)

    def test_message_includes_summary_when_provided(self) -> None:
        exc = CalendarAPIError(status_code=400, summary="Invalid time range")
        assert "Invalid time range" in str(exc)

    def test_message_without_summary(self) -> None:
        exc = CalendarAPIError(status_code=500)
        assert str(exc) == "Google Calendar API error 500"

    def test_no_token_in_message(self) -> None:
        # Ensure the access token never leaks into exception messages.
        exc = CalendarAPIError(status_code=401, summary="Unauthorized")
        assert _FAKE_ACCESS_TOKEN not in str(exc)


# ---------------------------------------------------------------------------
# _parse_datetime
# ---------------------------------------------------------------------------


class TestParseDatetime:
    def test_utc_z_suffix(self) -> None:
        dt = _parse_datetime("2026-03-07T15:00:00Z")
        assert dt == datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)

    def test_utc_plus00_offset(self) -> None:
        dt = _parse_datetime("2026-03-07T15:00:00+00:00")
        assert dt.tzinfo is not None
        assert dt.astimezone(timezone.utc) == datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)

    def test_non_utc_offset_converted_to_utc(self) -> None:
        # +05:00 means 10:00 UTC
        dt = _parse_datetime("2026-03-07T15:00:00+05:00")
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 10

    def test_date_only_string_treated_as_midnight_utc(self) -> None:
        dt = _parse_datetime("2026-03-08")
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 3, 8, 0, 0, 0, tzinfo=timezone.utc)

    def test_result_is_always_timezone_aware(self) -> None:
        dt = _parse_datetime("2026-03-07T15:00:00Z")
        assert dt.tzinfo is not None

    def test_result_timezone_is_utc(self) -> None:
        dt = _parse_datetime("2026-03-07T15:00:00+02:00")
        assert dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# _parse_event
# ---------------------------------------------------------------------------


class TestParseEvent:
    def test_full_event_id(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert event.id == "event-abc-123"

    def test_full_event_title(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert event.title == "Team standup"

    def test_full_event_start_is_utc(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert event.start == datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)

    def test_full_event_end_is_utc(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert event.end == datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)

    def test_full_event_description(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert event.description == "Daily sync"

    def test_full_event_location(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert event.location == "Conference room A"

    def test_full_event_url(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert event.url == "https://calendar.google.com/event?eid=abc123"

    def test_returns_calendar_event(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        assert isinstance(event, CalendarEvent)

    def test_missing_description_defaults_empty(self) -> None:
        event = _parse_event(_FAKE_EVENT_MINIMAL)
        assert event.description == ""

    def test_missing_location_defaults_empty(self) -> None:
        event = _parse_event(_FAKE_EVENT_MINIMAL)
        assert event.location == ""

    def test_missing_html_link_is_none(self) -> None:
        event = _parse_event(_FAKE_EVENT_MINIMAL)
        assert event.url is None

    def test_all_day_event_uses_date_field(self) -> None:
        event = _parse_event(_FAKE_ALL_DAY_EVENT)
        assert event.start == datetime(2026, 3, 8, 0, 0, 0, tzinfo=timezone.utc)

    def test_result_is_immutable(self) -> None:
        event = _parse_event(_FAKE_EVENT_DICT)
        with pytest.raises(Exception):
            event.title = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _build_event_body
# ---------------------------------------------------------------------------


class TestBuildEventBody:
    def test_summary_present(self) -> None:
        body = _build_event_body("My event", _START, _END, "", "")
        assert body["summary"] == "My event"

    def test_start_datetime_present(self) -> None:
        body = _build_event_body("My event", _START, _END, "", "")
        assert "dateTime" in body["start"]

    def test_end_datetime_present(self) -> None:
        body = _build_event_body("My event", _START, _END, "", "")
        assert "dateTime" in body["end"]

    def test_start_is_utc_iso(self) -> None:
        body = _build_event_body("My event", _START, _END, "", "")
        # UTC datetimes include +00:00 after astimezone conversion
        assert "2026-03-07" in body["start"]["dateTime"]
        assert "15:00:00" in body["start"]["dateTime"]

    def test_description_included_when_non_empty(self) -> None:
        body = _build_event_body("My event", _START, _END, "Notes here", "")
        assert body["description"] == "Notes here"

    def test_description_omitted_when_empty(self) -> None:
        body = _build_event_body("My event", _START, _END, "", "")
        assert "description" not in body

    def test_location_included_when_non_empty(self) -> None:
        body = _build_event_body("My event", _START, _END, "", "Room 1")
        assert body["location"] == "Room 1"

    def test_location_omitted_when_empty(self) -> None:
        body = _build_event_body("My event", _START, _END, "", "")
        assert "location" not in body

    def test_pure_function_same_output_for_same_input(self) -> None:
        a = _build_event_body("Test", _START, _END, "Desc", "Loc")
        b = _build_event_body("Test", _START, _END, "Desc", "Loc")
        assert a == b


# ---------------------------------------------------------------------------
# _auth_header
# ---------------------------------------------------------------------------


class TestAuthHeader:
    def test_returns_dict(self) -> None:
        result = _auth_header("my-token")
        assert isinstance(result, dict)

    def test_authorization_key_present(self) -> None:
        result = _auth_header("my-token")
        assert "Authorization" in result

    def test_bearer_prefix(self) -> None:
        result = _auth_header("my-token")
        assert result["Authorization"].startswith("Bearer ")

    def test_token_in_value(self) -> None:
        result = _auth_header("ya29.fake")
        assert result["Authorization"] == "Bearer ya29.fake"

    def test_pure_function(self) -> None:
        a = _auth_header("tok")
        b = _auth_header("tok")
        assert a == b


# ---------------------------------------------------------------------------
# _call_calendar_api
# ---------------------------------------------------------------------------


class TestCallCalendarApi:
    def _patch_requests(self, mock_response: MagicMock):
        return patch(
            "integrations.google_calendar.client.requests.request",
            return_value=mock_response,
        )

    def test_returns_parsed_json_on_success(self) -> None:
        data = {"items": [_FAKE_EVENT_DICT]}
        mock_resp = _make_mock_response(data, 200)
        with self._patch_requests(mock_resp):
            result = _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)
        assert result == data

    def test_raises_calendar_api_error_on_4xx(self) -> None:
        mock_resp = _make_mock_response(
            {"error": {"message": "Not found", "code": 404}}, 404
        )
        with self._patch_requests(mock_resp):
            with pytest.raises(CalendarAPIError) as exc_info:
                _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)
        assert exc_info.value.status_code == 404

    def test_raises_calendar_api_error_on_5xx(self) -> None:
        mock_resp = _make_mock_response({"error": {"message": "Server error", "code": 500}}, 500)
        with self._patch_requests(mock_resp):
            with pytest.raises(CalendarAPIError) as exc_info:
                _call_calendar_api("POST", "https://example.com", _FAKE_ACCESS_TOKEN)
        assert exc_info.value.status_code == 500

    def test_error_message_from_response_included(self) -> None:
        mock_resp = _make_mock_response(
            {"error": {"message": "Calendar usage limits exceeded.", "code": 429}}, 429
        )
        with self._patch_requests(mock_resp):
            with pytest.raises(CalendarAPIError) as exc_info:
                _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)
        assert "Calendar usage limits exceeded." in str(exc_info.value)

    def test_no_access_token_in_exception_message(self) -> None:
        mock_resp = _make_mock_response({"error": {"message": "Unauthorized"}}, 401)
        with self._patch_requests(mock_resp):
            with pytest.raises(CalendarAPIError) as exc_info:
                _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)
        assert _FAKE_ACCESS_TOKEN not in str(exc_info.value)

    def test_uses_bearer_authorization_header(self) -> None:
        data = {"items": []}
        mock_resp = _make_mock_response(data, 200)
        with patch(
            "integrations.google_calendar.client.requests.request",
            return_value=mock_resp,
        ) as mock_req:
            _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)
        _, kwargs = mock_req.call_args
        assert kwargs["headers"]["Authorization"] == f"Bearer {_FAKE_ACCESS_TOKEN}"

    def test_propagates_request_exception(self) -> None:
        with patch(
            "integrations.google_calendar.client.requests.request",
            side_effect=req.exceptions.Timeout("timed out"),
        ):
            with pytest.raises(req.exceptions.Timeout):
                _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)

    def test_sets_default_timeout(self) -> None:
        data = {"items": []}
        mock_resp = _make_mock_response(data, 200)
        with patch(
            "integrations.google_calendar.client.requests.request",
            return_value=mock_resp,
        ) as mock_req:
            _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)
        _, kwargs = mock_req.call_args
        assert "timeout" in kwargs

    def test_handles_non_json_error_body_gracefully(self) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 503
        mock_resp.json.side_effect = ValueError("no JSON")
        with patch("integrations.google_calendar.client.requests.request", return_value=mock_resp):
            with pytest.raises(CalendarAPIError) as exc_info:
                _call_calendar_api("GET", "https://example.com", _FAKE_ACCESS_TOKEN)
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# get_upcoming_events
# ---------------------------------------------------------------------------


class TestGetUpcomingEvents:
    def _patch_token(self, token):
        return patch(
            "integrations.google_calendar.client.get_valid_token",
            return_value=token,
        )

    def _patch_api(self, return_value=None, side_effect=None):
        if side_effect is not None:
            return patch(
                "integrations.google_calendar.client._call_calendar_api",
                side_effect=side_effect,
            )
        return patch(
            "integrations.google_calendar.client._call_calendar_api",
            return_value=return_value,
        )

    def test_returns_list_of_calendar_events(self) -> None:
        api_response = {"items": [_FAKE_EVENT_DICT, _FAKE_EVENT_MINIMAL]}
        with self._patch_token(_FAKE_TOKEN), self._patch_api(api_response):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert isinstance(events, list)
        assert all(isinstance(e, CalendarEvent) for e in events)

    def test_returns_correct_event_count(self) -> None:
        api_response = {"items": [_FAKE_EVENT_DICT, _FAKE_EVENT_MINIMAL]}
        with self._patch_token(_FAKE_TOKEN), self._patch_api(api_response):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert len(events) == 2

    def test_event_fields_populated(self) -> None:
        api_response = {"items": [_FAKE_EVENT_DICT]}
        with self._patch_token(_FAKE_TOKEN), self._patch_api(api_response):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        event = events[0]
        assert event.title == "Team standup"
        assert event.description == "Daily sync"
        assert event.location == "Conference room A"
        assert event.url is not None

    def test_empty_items_returns_empty_list(self) -> None:
        api_response = {"items": []}
        with self._patch_token(_FAKE_TOKEN), self._patch_api(api_response):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert events == []

    def test_missing_items_key_returns_empty_list(self) -> None:
        api_response = {}
        with self._patch_token(_FAKE_TOKEN), self._patch_api(api_response):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert events == []

    def test_auth_failure_returns_empty_list(self) -> None:
        with self._patch_token(None):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert events == []

    def test_api_error_returns_empty_list(self) -> None:
        with self._patch_token(_FAKE_TOKEN), self._patch_api(
            side_effect=CalendarAPIError(401, "Unauthorized")
        ):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert events == []

    def test_network_error_returns_empty_list(self) -> None:
        with self._patch_token(_FAKE_TOKEN), self._patch_api(
            side_effect=req.exceptions.ConnectionError("refused")
        ):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert events == []

    def test_days_parameter_affects_time_range(self) -> None:
        """The ``days`` parameter should be forwarded into the API params."""
        api_response = {"items": []}
        with self._patch_token(_FAKE_TOKEN):
            with patch(
                "integrations.google_calendar.client._call_calendar_api",
                return_value=api_response,
            ) as mock_api:
                get_upcoming_events(_FAKE_USER_ID, days=14, credentials=_FAKE_CREDENTIALS)
        _, kwargs = mock_api.call_args
        params = kwargs.get("params", {})
        # timeMax should be approximately now+14days (just verify timeMin < timeMax)
        time_min = datetime.fromisoformat(params["timeMin"])
        time_max = datetime.fromisoformat(params["timeMax"])
        delta = time_max - time_min
        assert delta.days >= 13  # allow small clock skew

    def test_passes_credentials_to_get_valid_token(self) -> None:
        api_response = {"items": []}
        with patch(
            "integrations.google_calendar.client.get_valid_token",
            return_value=_FAKE_TOKEN,
        ) as mock_gvt:
            with self._patch_api(api_response):
                get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        mock_gvt.assert_called_once()
        _, kwargs = mock_gvt.call_args
        assert kwargs.get("credentials") == _FAKE_CREDENTIALS

    def test_default_credentials_none_forwarded(self) -> None:
        api_response = {"items": []}
        with patch(
            "integrations.google_calendar.client.get_valid_token",
            return_value=_FAKE_TOKEN,
        ) as mock_gvt:
            with self._patch_api(api_response):
                get_upcoming_events(_FAKE_USER_ID)  # no credentials kwarg
        _, kwargs = mock_gvt.call_args
        assert kwargs.get("credentials") is None

    def test_timezone_aware_events_returned(self) -> None:
        api_response = {"items": [_FAKE_EVENT_DICT]}
        with self._patch_token(_FAKE_TOKEN), self._patch_api(api_response):
            events = get_upcoming_events(_FAKE_USER_ID, credentials=_FAKE_CREDENTIALS)
        assert events[0].start.tzinfo is not None
        assert events[0].end.tzinfo is not None


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


class TestCreateEvent:
    def _patch_token(self, token):
        return patch(
            "integrations.google_calendar.client.get_valid_token",
            return_value=token,
        )

    def _patch_api(self, return_value=None, side_effect=None):
        if side_effect is not None:
            return patch(
                "integrations.google_calendar.client._call_calendar_api",
                side_effect=side_effect,
            )
        return patch(
            "integrations.google_calendar.client._call_calendar_api",
            return_value=return_value,
        )

    def test_returns_calendar_event_on_success(self) -> None:
        with self._patch_token(_FAKE_TOKEN), self._patch_api(_FAKE_EVENT_DICT):
            result = create_event(
                _FAKE_USER_ID, "Team standup", _START, _END,
                credentials=_FAKE_CREDENTIALS,
            )
        assert isinstance(result, CalendarEvent)

    def test_returned_event_has_title(self) -> None:
        with self._patch_token(_FAKE_TOKEN), self._patch_api(_FAKE_EVENT_DICT):
            result = create_event(
                _FAKE_USER_ID, "Team standup", _START, _END,
                credentials=_FAKE_CREDENTIALS,
            )
        assert result.title == "Team standup"

    def test_returned_event_has_url(self) -> None:
        with self._patch_token(_FAKE_TOKEN), self._patch_api(_FAKE_EVENT_DICT):
            result = create_event(
                _FAKE_USER_ID, "Team standup", _START, _END,
                credentials=_FAKE_CREDENTIALS,
            )
        assert result.url == _FAKE_EVENT_DICT["htmlLink"]

    def test_default_end_is_start_plus_one_hour(self) -> None:
        """When end is None, the event body should use start + 1 hour."""
        with self._patch_token(_FAKE_TOKEN):
            with patch(
                "integrations.google_calendar.client._call_calendar_api",
                return_value=_FAKE_EVENT_DICT,
            ) as mock_api:
                create_event(
                    _FAKE_USER_ID, "Quick chat", _START,
                    credentials=_FAKE_CREDENTIALS,
                )
        _, kwargs = mock_api.call_args
        sent_body = kwargs.get("json", {})
        start_dt = datetime.fromisoformat(sent_body["start"]["dateTime"])
        end_dt = datetime.fromisoformat(sent_body["end"]["dateTime"])
        # Should be exactly 1 hour apart
        assert end_dt - start_dt == timedelta(hours=1)

    def test_explicit_end_time_used(self) -> None:
        custom_end = _START + timedelta(hours=2)
        with self._patch_token(_FAKE_TOKEN):
            with patch(
                "integrations.google_calendar.client._call_calendar_api",
                return_value=_FAKE_EVENT_DICT,
            ) as mock_api:
                create_event(
                    _FAKE_USER_ID, "Long meeting", _START, custom_end,
                    credentials=_FAKE_CREDENTIALS,
                )
        _, kwargs = mock_api.call_args
        sent_body = kwargs.get("json", {})
        start_dt = datetime.fromisoformat(sent_body["start"]["dateTime"])
        end_dt = datetime.fromisoformat(sent_body["end"]["dateTime"])
        assert end_dt - start_dt == timedelta(hours=2)

    def test_auth_failure_returns_none(self) -> None:
        with self._patch_token(None):
            result = create_event(
                _FAKE_USER_ID, "Meeting", _START,
                credentials=_FAKE_CREDENTIALS,
            )
        assert result is None

    def test_api_error_returns_none(self) -> None:
        with self._patch_token(_FAKE_TOKEN), self._patch_api(
            side_effect=CalendarAPIError(403, "Forbidden")
        ):
            result = create_event(
                _FAKE_USER_ID, "Meeting", _START,
                credentials=_FAKE_CREDENTIALS,
            )
        assert result is None

    def test_network_error_returns_none(self) -> None:
        with self._patch_token(_FAKE_TOKEN), self._patch_api(
            side_effect=req.exceptions.Timeout("timed out")
        ):
            result = create_event(
                _FAKE_USER_ID, "Meeting", _START,
                credentials=_FAKE_CREDENTIALS,
            )
        assert result is None

    def test_description_forwarded_to_api(self) -> None:
        with self._patch_token(_FAKE_TOKEN):
            with patch(
                "integrations.google_calendar.client._call_calendar_api",
                return_value=_FAKE_EVENT_DICT,
            ) as mock_api:
                create_event(
                    _FAKE_USER_ID, "Meeting", _START,
                    description="Agenda: review Q1 results",
                    credentials=_FAKE_CREDENTIALS,
                )
        _, kwargs = mock_api.call_args
        assert kwargs["json"]["description"] == "Agenda: review Q1 results"

    def test_location_forwarded_to_api(self) -> None:
        with self._patch_token(_FAKE_TOKEN):
            with patch(
                "integrations.google_calendar.client._call_calendar_api",
                return_value=_FAKE_EVENT_DICT,
            ) as mock_api:
                create_event(
                    _FAKE_USER_ID, "Meeting", _START,
                    location="Board room",
                    credentials=_FAKE_CREDENTIALS,
                )
        _, kwargs = mock_api.call_args
        assert kwargs["json"]["location"] == "Board room"

    def test_passes_credentials_to_get_valid_token(self) -> None:
        with patch(
            "integrations.google_calendar.client.get_valid_token",
            return_value=_FAKE_TOKEN,
        ) as mock_gvt:
            with self._patch_api(_FAKE_EVENT_DICT):
                create_event(
                    _FAKE_USER_ID, "Meeting", _START,
                    credentials=_FAKE_CREDENTIALS,
                )
        _, kwargs = mock_gvt.call_args
        assert kwargs.get("credentials") == _FAKE_CREDENTIALS

    def test_default_credentials_none_forwarded(self) -> None:
        with patch(
            "integrations.google_calendar.client.get_valid_token",
            return_value=_FAKE_TOKEN,
        ) as mock_gvt:
            with self._patch_api(_FAKE_EVENT_DICT):
                create_event(_FAKE_USER_ID, "Meeting", _START)
        _, kwargs = mock_gvt.call_args
        assert kwargs.get("credentials") is None

    def test_api_called_with_post_method(self) -> None:
        with self._patch_token(_FAKE_TOKEN):
            with patch(
                "integrations.google_calendar.client._call_calendar_api",
                return_value=_FAKE_EVENT_DICT,
            ) as mock_api:
                create_event(_FAKE_USER_ID, "Meeting", _START, credentials=_FAKE_CREDENTIALS)
        method = mock_api.call_args[0][0]
        assert method == "POST"


# ---------------------------------------------------------------------------
# gcal_add_link re-export
# ---------------------------------------------------------------------------


class TestGcalAddLinkReexport:
    def test_importable_from_client_module(self) -> None:
        """Verify gcal_add_link is importable from the client module."""
        assert callable(gcal_add_link)

    def test_generates_a_url(self) -> None:
        url = gcal_add_link("Test event", _START, _END)
        assert url.startswith("https://calendar.google.com/calendar/r/eventedit")

    def test_title_in_url(self) -> None:
        url = gcal_add_link("My Meeting", _START, _END)
        assert "My+Meeting" in url or "My%20Meeting" in url or "My Meeting" in url

    def test_dates_in_url(self) -> None:
        url = gcal_add_link("Event", _START, _END)
        # Google Calendar compact date format: 20260307T150000Z
        assert "20260307T150000Z" in url
