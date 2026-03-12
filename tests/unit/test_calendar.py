"""
Tests for the calendar deep-link utility (src/utils/calendar.py).

Covers URL structure, parameter encoding, default end-time fallback,
timezone normalisation, and the markdown wrapper.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

# Make src/utils importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from utils.calendar import (
    _format_gcal_datetime,
    gcal_add_link,
    gcal_add_link_md,
)


# =============================================================================
# _format_gcal_datetime
# =============================================================================


class TestFormatGcalDatetime:
    def test_utc_aware_datetime(self):
        dt = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        assert _format_gcal_datetime(dt) == "20260307T150000Z"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2026, 3, 7, 19, 30, 0)
        assert _format_gcal_datetime(dt) == "20260307T193000Z"

    def test_non_utc_timezone_converted(self):
        # Eastern Standard Time is UTC-5
        from datetime import timezone as tz
        est = tz(timedelta(hours=-5))
        dt = datetime(2026, 3, 7, 10, 0, 0, tzinfo=est)  # 10:00 EST = 15:00 UTC
        assert _format_gcal_datetime(dt) == "20260307T150000Z"

    def test_seconds_included(self):
        dt = datetime(2026, 1, 1, 0, 0, 45, tzinfo=timezone.utc)
        assert _format_gcal_datetime(dt) == "20260101T000045Z"


# =============================================================================
# gcal_add_link
# =============================================================================


class TestGcalAddLink:
    def _parse(self, url: str) -> dict:
        """Parse URL into (base, query_params) for assertions."""
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        params = parse_qs(parsed.query)
        # parse_qs returns lists — unwrap single values
        return {"base": base, "params": {k: v[0] for k, v in params.items()}}

    def test_base_url(self):
        start = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Test", start))
        assert result["base"] == "https://calendar.google.com/calendar/r/eventedit"

    def test_title_encoded_in_text_param(self):
        start = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Doctor appointment", start))
        assert result["params"]["text"] == "Doctor appointment"

    def test_dates_param_format(self):
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Appt", start, end))
        assert result["params"]["dates"] == "20260307T150000Z/20260307T160000Z"

    def test_default_end_is_one_hour_after_start(self):
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Appt", start))
        expected_dates = "20260307T150000Z/20260307T160000Z"
        assert result["params"]["dates"] == expected_dates

    def test_description_included_when_provided(self):
        start = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Appt", start, description="Annual checkup"))
        assert result["params"]["details"] == "Annual checkup"

    def test_description_omitted_when_empty(self):
        start = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Appt", start))
        assert "details" not in result["params"]

    def test_location_included_when_provided(self):
        start = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Appt", start, location="123 Main St"))
        assert result["params"]["location"] == "123 Main St"

    def test_location_omitted_when_empty(self):
        start = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        result = self._parse(gcal_add_link("Appt", start))
        assert "location" not in result["params"]

    def test_doctor_appointment_sample(self):
        """Canonical sample: Doctor appointment, March 7 2026 3pm-4pm UTC."""
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        url = gcal_add_link("Doctor appointment", start, end)
        result = self._parse(url)
        assert result["params"]["text"] == "Doctor appointment"
        assert result["params"]["dates"] == "20260307T150000Z/20260307T160000Z"
        # Verify the full URL matches expected output
        expected = (
            "https://calendar.google.com/calendar/r/eventedit"
            "?text=Doctor+appointment"
            "&dates=20260307T150000Z%2F20260307T160000Z"
        )
        assert url == expected

    def test_title_with_special_characters_encoded(self):
        start = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
        url = gcal_add_link("Q&A Session: 'Hello World'", start)
        assert "Q%26A" in url or "Q&A" in url  # urlencode handles this
        # Most importantly, parse back correctly
        result = self._parse(url)
        assert result["params"]["text"] == "Q&A Session: 'Hello World'"

    def test_all_params_combined(self):
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        url = gcal_add_link(
            "Doctor appointment",
            start,
            end,
            description="Annual physical",
            location="123 Main St, Springfield",
        )
        result = self._parse(url)
        assert result["params"]["text"] == "Doctor appointment"
        assert result["params"]["dates"] == "20260307T150000Z/20260307T160000Z"
        assert result["params"]["details"] == "Annual physical"
        assert result["params"]["location"] == "123 Main St, Springfield"


# =============================================================================
# gcal_add_link_md
# =============================================================================


class TestGcalAddLinkMd:
    def test_returns_markdown_link_format(self):
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        result = gcal_add_link_md("Doctor appointment", start)
        assert result.startswith("[Add to Google Calendar](")
        assert result.endswith(")")

    def test_label_text(self):
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        result = gcal_add_link_md("Appt", start)
        assert result.startswith("[Add to Google Calendar]")

    def test_url_is_valid_gcal_link(self):
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        result = gcal_add_link_md("Appt", start)
        # Extract URL from markdown
        url = result[len("[Add to Google Calendar]("):-1]
        assert url.startswith("https://calendar.google.com/calendar/r/eventedit")

    def test_passes_kwargs_through(self):
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        result = gcal_add_link_md(
            "Doctor appointment",
            start,
            end=end,
            description="Annual checkup",
            location="Clinic",
        )
        assert "details=Annual+checkup" in result
        assert "location=Clinic" in result

    def test_doctor_appointment_sample_md(self):
        """Canonical sample output for documentation purposes."""
        start = datetime(2026, 3, 7, 15, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc)
        result = gcal_add_link_md("Doctor appointment", start, end)
        expected = (
            "[Add to Google Calendar]"
            "(https://calendar.google.com/calendar/r/eventedit"
            "?text=Doctor+appointment"
            "&dates=20260307T150000Z%2F20260307T160000Z)"
        )
        assert result == expected
