"""
Tests for issue #1665 additions to event_bus.py:
- CRITICAL severity level accepted by EventFilter
- CriticalAlertListener: always forwards critical events to Telegram outbox,
  no debug-mode gate, suppressed by LOBSTER_SILENT_ERRORS=true
- MetricsListener: in-memory counters for events_by_type, events_by_severity,
  errors_last_1h

Tests are named after behavior, not mechanism.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from event_bus import (
    CriticalAlertListener,
    EventBus,
    EventFilter,
    LobsterEvent,
    MetricsListener,
    VALID_SEVERITIES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(
    event_type: str = "test.event",
    severity: str = "info",
    source: str = "test",
    payload: dict | None = None,
    timestamp: datetime | None = None,
) -> LobsterEvent:
    kwargs = dict(
        event_type=event_type,
        severity=severity,
        source=source,
        payload=payload or {"msg": "hello"},
    )
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return LobsterEvent(**kwargs)


# ---------------------------------------------------------------------------
# CRITICAL severity level
# ---------------------------------------------------------------------------

class TestCriticalSeverityLevel:
    """CRITICAL is a valid, accepted severity in the event system."""

    def test_critical_is_in_valid_severities_set(self):
        assert "critical" in VALID_SEVERITIES

    def test_event_filter_accepts_critical_severity(self):
        f = EventFilter(severity={"critical"}, event_types={"*"})
        assert f.accepts(make_event(severity="critical"))

    def test_event_filter_blocks_critical_when_not_in_severity_set(self):
        f = EventFilter(severity={"info", "warn", "error"}, event_types={"*"})
        assert not f.accepts(make_event(severity="critical"))

    def test_default_event_filter_accepts_critical(self):
        # The default filter (all severities) must include critical
        f = EventFilter()
        assert f.accepts(make_event(severity="critical"))


# ---------------------------------------------------------------------------
# CriticalAlertListener
# ---------------------------------------------------------------------------

class TestCriticalAlertListener:
    """CriticalAlertListener forwards critical-severity events to the Telegram outbox."""

    def test_accepts_critical_severity_event(self):
        listener = CriticalAlertListener()
        assert listener.accepts(make_event(severity="critical"))

    def test_rejects_non_critical_severity(self):
        listener = CriticalAlertListener()
        for sev in ("debug", "info", "warn", "error"):
            assert not listener.accepts(make_event(severity=sev)), (
                f"CriticalAlertListener must not accept severity={sev!r}"
            )

    def test_delivers_without_debug_mode_gate(self):
        """Critical alerts must reach Telegram even when LOBSTER_DEBUG is false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_dir = Path(tmpdir) / "outbox"
            listener = CriticalAlertListener(outbox_dir=outbox_dir)
            with patch.object(listener, "_resolve_owner", return_value=(12345, "telegram")):
                with patch.dict(os.environ, {"LOBSTER_DEBUG": "false"}):
                    asyncio.run(listener.deliver(make_event(severity="critical")))
            files = list(outbox_dir.iterdir())
            assert len(files) == 1, "Expected 1 outbox file even without debug mode"

    def test_suppressed_when_lobster_silent_errors_is_true(self):
        """LOBSTER_SILENT_ERRORS=true must suppress delivery."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_dir = Path(tmpdir) / "outbox"
            listener = CriticalAlertListener(outbox_dir=outbox_dir)
            with patch.object(listener, "_resolve_owner", return_value=(12345, "telegram")):
                with patch.dict(os.environ, {"LOBSTER_SILENT_ERRORS": "true"}):
                    asyncio.run(listener.deliver(make_event(severity="critical")))
            assert not outbox_dir.exists(), "No file must be written when LOBSTER_SILENT_ERRORS=true"

    def test_delivery_writes_valid_outbox_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_dir = Path(tmpdir) / "outbox"
            listener = CriticalAlertListener(outbox_dir=outbox_dir)
            with patch.object(listener, "_resolve_owner", return_value=(9999, "telegram")):
                asyncio.run(listener.deliver(make_event(severity="critical", event_type="system.error")))
            files = list(outbox_dir.iterdir())
            content = json.loads(files[0].read_text())
            assert content["chat_id"] == 9999
            assert "critical" in content["text"].lower() or "system.error" in content["text"]

    def test_no_op_when_no_chat_id_resolved(self):
        """When owner cannot be resolved, no outbox file is written."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_dir = Path(tmpdir) / "outbox"
            listener = CriticalAlertListener(outbox_dir=outbox_dir)
            with patch.object(listener, "_resolve_owner", return_value=(None, "telegram")):
                asyncio.run(listener.deliver(make_event(severity="critical")))
            assert not outbox_dir.exists()

    def test_delivery_failure_does_not_raise(self):
        """A broken outbox path must not propagate an exception."""
        listener = CriticalAlertListener(outbox_dir=Path("/proc/nonexistent/outbox"))
        with patch.object(listener, "_resolve_owner", return_value=(12345, "telegram")):
            # Must not raise
            asyncio.run(listener.deliver(make_event(severity="critical")))

    def test_init_event_bus_registers_critical_alert_listener(self):
        """init_event_bus must register a CriticalAlertListener by default."""
        import event_bus as _eb
        _eb._EVENT_BUS = None
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            bus = _eb.init_event_bus(jsonl_path=path)
        listener_names = [getattr(l, "name", None) for l in bus._listeners]
        assert "critical-alert" in listener_names, (
            "init_event_bus must register a CriticalAlertListener named 'critical-alert'"
        )


# ---------------------------------------------------------------------------
# MetricsListener
# ---------------------------------------------------------------------------

class TestMetricsListener:
    """MetricsListener maintains in-memory counters and computes errors_last_1h."""

    def test_accepts_all_events_by_default(self):
        m = MetricsListener()
        for sev in ("debug", "info", "warn", "error", "critical"):
            assert m.accepts(make_event(severity=sev))

    def test_counts_by_event_type(self):
        m = MetricsListener()
        asyncio.run(m.deliver(make_event(event_type="agent.spawn")))
        asyncio.run(m.deliver(make_event(event_type="agent.spawn")))
        asyncio.run(m.deliver(make_event(event_type="memory.write")))
        snapshot = m.get_snapshot()
        assert snapshot["events_by_type"]["agent.spawn"] == 2
        assert snapshot["events_by_type"]["memory.write"] == 1

    def test_counts_by_severity(self):
        m = MetricsListener()
        asyncio.run(m.deliver(make_event(severity="info")))
        asyncio.run(m.deliver(make_event(severity="error")))
        asyncio.run(m.deliver(make_event(severity="error")))
        asyncio.run(m.deliver(make_event(severity="critical")))
        snapshot = m.get_snapshot()
        assert snapshot["events_by_severity"]["info"] == 1
        assert snapshot["events_by_severity"]["error"] == 2
        assert snapshot["events_by_severity"]["critical"] == 1

    def test_errors_last_1h_includes_recent_errors(self):
        m = MetricsListener()
        recent_ts = datetime.now(timezone.utc)
        asyncio.run(m.deliver(make_event(severity="error", timestamp=recent_ts)))
        asyncio.run(m.deliver(make_event(severity="critical", timestamp=recent_ts)))
        snapshot = m.get_snapshot()
        assert snapshot["errors_last_1h"] == 2

    def test_errors_last_1h_excludes_old_errors(self):
        m = MetricsListener()
        old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        asyncio.run(m.deliver(make_event(severity="error", timestamp=old_ts)))
        snapshot = m.get_snapshot()
        # Error was 2h ago — must not appear in errors_last_1h
        assert snapshot["errors_last_1h"] == 0

    def test_snapshot_is_a_fresh_copy(self):
        """get_snapshot returns a copy — mutating it does not affect internal state."""
        m = MetricsListener()
        asyncio.run(m.deliver(make_event(event_type="agent.spawn")))
        snap1 = m.get_snapshot()
        snap1["events_by_type"]["agent.spawn"] = 9999
        snap2 = m.get_snapshot()
        assert snap2["events_by_type"]["agent.spawn"] == 1

    def test_delivery_failure_does_not_raise(self):
        """MetricsListener.deliver() must not propagate exceptions."""
        m = MetricsListener()
        # Deliver an event that has an odd payload — must not raise
        asyncio.run(m.deliver(make_event()))

    def test_init_event_bus_registers_metrics_listener(self):
        """init_event_bus must register a MetricsListener."""
        import event_bus as _eb
        _eb._EVENT_BUS = None
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            bus = _eb.init_event_bus(jsonl_path=path)
        listener_names = [getattr(l, "name", None) for l in bus._listeners]
        assert "metrics" in listener_names, (
            "init_event_bus must register a MetricsListener named 'metrics'"
        )

    def test_get_metrics_listener_from_bus(self):
        """get_metrics_listener returns the registered MetricsListener."""
        import event_bus as _eb
        _eb._EVENT_BUS = None
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            _eb.init_event_bus(jsonl_path=path)
        ml = _eb.get_metrics_listener()
        assert ml is not None
        assert isinstance(ml, MetricsListener)
