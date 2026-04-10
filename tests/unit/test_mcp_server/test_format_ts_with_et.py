"""
Tests for _format_ts_with_local_tz — the owner-tz-aware timestamp formatter
used in handle_check_inbox output.

Verifies that:
- Timestamps are formatted using the owner's timezone from owner.toml
- DST transitions work correctly (EDT in summer, EST in winter when set to NY)
- UTC is the fallback when no timezone is configured in owner.toml
- Microseconds are stripped for cleaner display
- Bad/malformed timestamps fall back to the raw string unchanged
"""

import sys
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from inbox_server import _format_ts_with_local_tz  # noqa: E402


class TestFormatTsWithLocalTz:
    def test_eastern_summer_shows_edt(self):
        # With NY timezone: April is EDT (UTC-4), 14:32 UTC -> 10:32 AM EDT
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/New_York")):
            result = _format_ts_with_local_tz("2026-04-10T14:32:18.059908")
        assert result == "2026-04-10T14:32:18 UTC (10:32 AM EDT)"

    def test_eastern_winter_shows_est(self):
        # With NY timezone: January is EST (UTC-5), 20:00 UTC -> 3:00 PM EST
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/New_York")):
            result = _format_ts_with_local_tz("2026-01-15T20:00:00")
        assert result == "2026-01-15T20:00:00 UTC (3:00 PM EST)"

    def test_pacific_timezone_shows_pdt(self):
        # With LA timezone: April is PDT (UTC-7), 14:32 UTC -> 7:32 AM PDT
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/Los_Angeles")):
            result = _format_ts_with_local_tz("2026-04-10T14:32:18")
        assert result == "2026-04-10T14:32:18 UTC (7:32 AM PDT)"

    def test_utc_fallback_when_no_timezone_set(self):
        # With UTC (the fallback): time is identical, suffix is UTC
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("UTC")):
            result = _format_ts_with_local_tz("2026-04-10T14:32:18")
        assert result == "2026-04-10T14:32:18 UTC (2:32 PM UTC)"

    def test_explicit_utc_offset_handled(self):
        # Explicit +00:00 suffix should produce the same result as naive UTC
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/New_York")):
            result = _format_ts_with_local_tz("2026-04-10T14:32:18+00:00")
        assert result == "2026-04-10T14:32:18 UTC (10:32 AM EDT)"

    def test_microseconds_stripped_from_utc_portion(self):
        # Sub-second precision should be stripped in the displayed UTC part
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/New_York")):
            result = _format_ts_with_local_tz("2026-04-10T14:32:18.123456")
        assert ".123456" not in result
        assert "14:32:18 UTC" in result

    def test_midnight_utc_formats_correctly(self):
        # Midnight UTC in summer with NY tz -> 8:00 PM EDT previous day (UTC-4)
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/New_York")):
            result = _format_ts_with_local_tz("2026-06-01T00:00:00")
        assert result == "2026-06-01T00:00:00 UTC (8:00 PM EDT)"

    def test_malformed_timestamp_returns_raw_string(self):
        bad = "not-a-timestamp"
        result = _format_ts_with_local_tz(bad)
        assert result == bad

    def test_empty_string_returns_empty_string(self):
        result = _format_ts_with_local_tz("")
        assert result == ""

    def test_utc_label_always_present(self):
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/New_York")):
            result = _format_ts_with_local_tz("2026-04-10T14:32:18")
        assert " UTC " in result

    def test_format_contains_parenthesized_local_time(self):
        with patch("inbox_server._get_display_tz", return_value=ZoneInfo("America/New_York")):
            result = _format_ts_with_local_tz("2026-04-10T14:32:18")
        assert result.startswith("2026-04-10T14:32:18 UTC (")
        assert result.endswith(")")
