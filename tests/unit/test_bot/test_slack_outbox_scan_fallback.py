"""
Tests for issue #1935: outbox fallback scanner in slack_router.py.

The watchdog Observer can die silently when:
  - The inotify kernel queue overflows (IN_Q_OVERFLOW)
  - An unhandled exception kills the observer thread

These tests verify the periodic fallback scanner that protects against
missed outbox files and surfaces observer failures in logs.

All Slack API calls are mocked — no production tokens are used.
"""

import logging
import os
import sys
import time
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is importable
_SRC = Path(__file__).parent.parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Named constants (match the issue spec and implementation constants)
# ---------------------------------------------------------------------------

OUTBOX_SCAN_INTERVAL_DEFAULT = 30  # seconds — default from LOBSTER_SLACK_OUTBOX_SCAN_INTERVAL
FAKE_BOT_CHANNEL = "DBOT000001"
FAKE_USER_CHANNEL = "DUSER000001"


# ---------------------------------------------------------------------------
# Module loader (reused from test_slack_router_config.py pattern)
# ---------------------------------------------------------------------------

def _make_slack_mocks():
    """Return module-level mock objects for all Slack dependencies."""
    mock_app = MagicMock()
    mock_app.event.side_effect = lambda *a, **kw: (lambda fn: fn)
    mock_app_cls = MagicMock(return_value=mock_app)
    mock_bolt_mod = MagicMock()
    mock_bolt_mod.App = mock_app_cls

    mock_sm_handler_cls = MagicMock()
    mock_socket_mod = MagicMock()
    mock_socket_mod.SocketModeHandler = mock_sm_handler_cls

    mock_bot_client = MagicMock()
    mock_bot_client.auth_test.return_value = {
        "user_id": "UBOTAPP001",
        "user": "lobster-bot",
    }

    mock_user_client = MagicMock()
    mock_user_client.auth_test.return_value = {
        "user_id": "USELF00001",
        "user": "lobster-user",
    }

    _call_count = [0]

    def _webclient_side_effect(token=None, **kw):
        _call_count[0] += 1
        if _call_count[0] == 1:
            return mock_bot_client
        return mock_user_client

    mock_webclient_cls = MagicMock(side_effect=_webclient_side_effect)
    mock_sdk_mod = MagicMock()
    mock_sdk_mod.WebClient = mock_webclient_cls

    mock_errors_mod = MagicMock()
    mock_watchdog_obs = MagicMock()
    mock_watchdog_events = MagicMock()

    return {
        "slack_bolt": mock_bolt_mod,
        "slack_bolt.adapter": MagicMock(),
        "slack_bolt.adapter.socket_mode": mock_socket_mod,
        "slack_sdk": mock_sdk_mod,
        "slack_sdk.errors": mock_errors_mod,
        "watchdog": MagicMock(),
        "watchdog.observers": mock_watchdog_obs,
        "watchdog.events": mock_watchdog_events,
        "_bot_client": mock_bot_client,
        "_user_client": mock_user_client,
    }


def _minimal_env(**overrides):
    base = {
        "LOBSTER_SLACK_BOT_TOKEN": "xoxb-fake-bot-token",
        "LOBSTER_SLACK_APP_TOKEN": "xapp-fake-app-token",
        "LOBSTER_SLACK_USER_TOKEN": "xoxp-fake-user-token",
        "LOBSTER_SLACK_POLL_CHANNELS": FAKE_USER_CHANNEL,
        "LOBSTER_SLACK_CHANNEL_REMAP": f"{FAKE_BOT_CHANNEL}:{FAKE_USER_CHANNEL}",
        "LOBSTER_MESSAGES": "/tmp/lobster-test-messages",
        "LOBSTER_WORKSPACE": "/tmp/lobster-test-workspace",
    }
    base.update(overrides)
    return base


def _load_module(env: dict):
    """Reload slack_router under a clean patched environment."""
    for key in list(sys.modules.keys()):
        if "slack_router" in key or key == "src.bot.slack_router":
            del sys.modules[key]

    mocks = _make_slack_mocks()
    module_patches = {k: v for k, v in mocks.items() if not k.startswith("_")}

    mock_outbox_mod = MagicMock()
    mock_outbox_mod.OutboxFileHandler = MagicMock()
    mock_outbox_mod.OutboxWatcher = MagicMock()
    mock_outbox_mod.drain_outbox = MagicMock()
    module_patches["channels.outbox"] = mock_outbox_mod

    null_handler = logging.NullHandler()

    with patch.dict(sys.modules, module_patches), \
         patch.dict(os.environ, env, clear=True), \
         patch("pathlib.Path.mkdir"), \
         patch("logging.handlers.RotatingFileHandler", return_value=null_handler):
        import src.bot.slack_router as m
        m._test_outbox_mod = mock_outbox_mod
        return m


# ---------------------------------------------------------------------------
# 1. OUTBOX_SCAN_INTERVAL constant exposed and configurable
# ---------------------------------------------------------------------------

class TestOutboxScanIntervalConfig:
    """LOBSTER_SLACK_OUTBOX_SCAN_INTERVAL controls the fallback scan period."""

    def test_default_scan_interval_is_30_seconds(self):
        """When no env var is set, the scan interval defaults to 30 seconds."""
        m = _load_module(_minimal_env())
        assert m.OUTBOX_SCAN_INTERVAL == OUTBOX_SCAN_INTERVAL_DEFAULT

    def test_scan_interval_overridable_via_env_var(self):
        """LOBSTER_SLACK_OUTBOX_SCAN_INTERVAL sets the scan interval."""
        m = _load_module(_minimal_env(LOBSTER_SLACK_OUTBOX_SCAN_INTERVAL="60"))
        assert m.OUTBOX_SCAN_INTERVAL == 60

    def test_scan_interval_is_integer(self):
        """The parsed scan interval is an integer, not a string."""
        m = _load_module(_minimal_env(LOBSTER_SLACK_OUTBOX_SCAN_INTERVAL="45"))
        assert isinstance(m.OUTBOX_SCAN_INTERVAL, int)


# ---------------------------------------------------------------------------
# 2. _scan_outbox_periodically calls drain_outbox on each tick
# ---------------------------------------------------------------------------

class TestScanOutboxPeriodically:
    """_scan_outbox_periodically drains the outbox on every interval tick."""

    def test_scan_calls_drain_outbox_on_first_tick(self):
        """The scan function calls drain_outbox at least once before being stopped."""
        m = _load_module(_minimal_env())

        stop = Event()
        drain_calls = []

        def fake_drain(outbox_dir, *, source, send_fn, log):
            drain_calls.append((outbox_dir, source))

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True

        # Stop after first drain call
        original_wait = Event.wait

        call_count = [0]

        def fake_scan_wait(self_evt, timeout=None):
            call_count[0] += 1
            # Signal stop after the first wait so the loop exits cleanly
            stop.set()

        with patch.object(m, "drain_outbox", fake_drain), \
             patch("threading.Event.wait", fake_scan_wait):
            m._scan_outbox_periodically(stop, mock_observer)

        assert len(drain_calls) >= 1, "drain_outbox should be called at least once"
        # Check drain was called with source="slack"
        sources = [src for _, src in drain_calls]
        assert all(s == "slack" for s in sources), (
            f"Expected all drain_outbox calls to have source='slack', got: {sources}"
        )

    def test_scan_passes_send_slack_reply_to_drain(self):
        """drain_outbox is called with the module's _send_slack_reply function."""
        m = _load_module(_minimal_env())

        stop = Event()
        drain_send_fns = []

        def fake_drain(outbox_dir, *, source, send_fn, log):
            drain_send_fns.append(send_fn)
            stop.set()  # stop after first call

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True

        with patch.object(m, "drain_outbox", fake_drain):
            m._scan_outbox_periodically(stop, mock_observer)

        assert len(drain_send_fns) >= 1
        assert drain_send_fns[0] is m._send_slack_reply, (
            "drain_outbox should be called with _send_slack_reply"
        )

    def test_scan_stops_when_stop_event_set(self):
        """The scan loop exits when the stop event is set before the first tick."""
        m = _load_module(_minimal_env())

        stop = Event()
        stop.set()  # Pre-set: loop should exit without calling drain

        drain_calls = []

        def fake_drain(outbox_dir, *, source, send_fn, log):
            drain_calls.append(True)

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True

        with patch.object(m, "drain_outbox", fake_drain):
            m._scan_outbox_periodically(stop, mock_observer)

        assert len(drain_calls) == 0, (
            "drain_outbox should not be called when stop is already set"
        )


# ---------------------------------------------------------------------------
# 3. Observer health check: warning logged when observer thread dies
# ---------------------------------------------------------------------------

class TestObserverHealthCheck:
    """The scan loop logs a warning when the observer thread is no longer alive."""

    def test_logs_warning_when_observer_dead(self, caplog):
        """When observer.is_alive() returns False, a WARNING is logged."""
        m = _load_module(_minimal_env())

        stop = Event()

        # drain is a no-op; we only care about the health check log
        def fake_drain(outbox_dir, *, source, send_fn, log):
            stop.set()  # stop after first tick

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = False  # observer is dead

        with patch.object(m, "drain_outbox", fake_drain), \
             caplog.at_level(logging.WARNING, logger="lobster-slack"):
            m._scan_outbox_periodically(stop, mock_observer)

        warning_text = caplog.text.lower()
        assert (
            "observer" in warning_text or "watcher" in warning_text
        ), f"Expected observer-dead warning, got: {caplog.text}"
        assert any(
            r.levelno >= logging.WARNING for r in caplog.records
        ), "Expected at least one WARNING log record"

    def test_no_warning_when_observer_alive(self, caplog):
        """When observer.is_alive() returns True, no observer-dead warning is logged."""
        m = _load_module(_minimal_env())

        stop = Event()

        def fake_drain(outbox_dir, *, source, send_fn, log):
            stop.set()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True  # observer is healthy

        with patch.object(m, "drain_outbox", fake_drain), \
             caplog.at_level(logging.WARNING, logger="lobster-slack"):
            m._scan_outbox_periodically(stop, mock_observer)

        for record in caplog.records:
            if record.levelno >= logging.WARNING:
                msg = record.message.lower()
                assert "observer" not in msg and "watcher" not in msg, (
                    f"Unexpected observer warning when observer is alive: {record.message}"
                )

    def test_observer_health_checked_on_every_tick(self):
        """observer.is_alive() is called once per scan tick, not just at startup."""
        m = _load_module(_minimal_env())

        stop = Event()
        tick_count = [0]
        max_ticks = 3

        def fake_drain(outbox_dir, *, source, send_fn, log):
            tick_count[0] += 1
            if tick_count[0] >= max_ticks:
                stop.set()

        mock_observer = MagicMock()
        mock_observer.is_alive.return_value = True

        with patch.object(m, "drain_outbox", fake_drain):
            m._scan_outbox_periodically(stop, mock_observer)

        # is_alive should be called once per tick
        assert mock_observer.is_alive.call_count >= max_ticks, (
            f"Expected is_alive() called at least {max_ticks} times, "
            f"got {mock_observer.is_alive.call_count}"
        )
