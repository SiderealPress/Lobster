"""
Tests for _format_ts_with_et — the ET timestamp formatter added to handle_check_inbox output.

Verifies that:
- Naive UTC timestamps get the ET equivalent appended (EDT in summer, EST in winter)
- Explicit UTC timestamps (with +00:00) are handled correctly
- Microseconds are stripped for cleaner display
- Bad/malformed timestamps fall back to the raw string unchanged
"""

import sys
from pathlib import Path

import pytest

_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from inbox_server import _format_ts_with_et  # noqa: E402


class TestFormatTsWithEt:
    def test_summer_timestamp_shows_edt(self):
        # April is EDT (UTC-4): 14:32 UTC -> 10:32 AM EDT
        result = _format_ts_with_et("2026-04-10T14:32:18.059908")
        assert result == "2026-04-10T14:32:18 UTC (10:32 AM EDT)"

    def test_winter_timestamp_shows_est(self):
        # January is EST (UTC-5): 20:00 UTC -> 3:00 PM EST
        result = _format_ts_with_et("2026-01-15T20:00:00")
        assert result == "2026-01-15T20:00:00 UTC (3:00 PM EST)"

    def test_explicit_utc_offset_handled(self):
        # Explicit +00:00 suffix should produce the same result as naive UTC
        result = _format_ts_with_et("2026-04-10T14:32:18+00:00")
        assert result == "2026-04-10T14:32:18 UTC (10:32 AM EDT)"

    def test_microseconds_stripped_from_utc_portion(self):
        # Sub-second precision should be stripped in the displayed UTC part
        result = _format_ts_with_et("2026-04-10T14:32:18.123456")
        assert ".123456" not in result
        assert "14:32:18 UTC" in result

    def test_midnight_utc_formats_correctly(self):
        # Midnight UTC in summer -> 8:00 PM EDT previous day (UTC-4)
        result = _format_ts_with_et("2026-06-01T00:00:00")
        assert result == "2026-06-01T00:00:00 UTC (8:00 PM EDT)"

    def test_malformed_timestamp_returns_raw_string(self):
        bad = "not-a-timestamp"
        result = _format_ts_with_et(bad)
        assert result == bad

    def test_empty_string_returns_empty_string(self):
        result = _format_ts_with_et("")
        assert result == ""

    def test_utc_label_always_present(self):
        result = _format_ts_with_et("2026-04-10T14:32:18")
        assert " UTC " in result

    def test_et_label_always_present(self):
        # Result contains EDT (summer) or EST (winter) — both end in T
        result = _format_ts_with_et("2026-04-10T14:32:18")
        assert "EDT" in result or "EST" in result

    def test_format_contains_parenthesized_et(self):
        result = _format_ts_with_et("2026-04-10T14:32:18")
        assert result.startswith("2026-04-10T14:32:18 UTC (")
        assert result.endswith(")")
