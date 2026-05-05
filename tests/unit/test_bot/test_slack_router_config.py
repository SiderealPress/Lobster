"""
Tests for slack_router.py config-driven parameterization (PR 1).

These tests verify:
1. parse_channel_remap parses LOBSTER_SLACK_CHANNEL_REMAP into a dict
2. The self-user ID is resolved dynamically from auth.test, not hardcoded
3. A startup warning fires when LOBSTER_SLACK_USER_TOKEN is set but
   LOBSTER_SLACK_POLL_CHANNELS is empty
4. Inbound channel remap uses the config-driven dict
5. Outbound channel remap uses the config-driven dict

All Slack API calls are mocked — no production tokens are used.
Fake/placeholder channel and user IDs are used throughout.
"""

import importlib
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import logging
import pytest

# Ensure src is importable
_SRC = Path(__file__).parent.parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Fake / placeholder IDs — no real workspace values appear anywhere in tests
# ---------------------------------------------------------------------------

FAKE_BOT_CHANNEL = "DBOT000001"    # placeholder for bot-DM channel
FAKE_USER_CHANNEL = "DUSER000001"  # placeholder for user-DM channel
FAKE_SELF_USER_ID = "USELF00001"   # placeholder xoxp- identity


# ---------------------------------------------------------------------------
# Module-loader helper
# ---------------------------------------------------------------------------

def _make_slack_mocks():
    """Return a dict of module-level mock objects for all Slack dependencies."""
    # slack_bolt mocks — the app.event() decorator must be transparent so that
    # handle_message_events stays as the original function in the module namespace.
    # Use a side_effect that returns the decorated function unchanged.
    mock_app = MagicMock()
    mock_app.event.side_effect = lambda *a, **kw: (lambda fn: fn)
    mock_app_cls = MagicMock(return_value=mock_app)
    mock_bolt_mod = MagicMock()
    mock_bolt_mod.App = mock_app_cls

    mock_sm_handler_cls = MagicMock()
    mock_socket_mod = MagicMock()
    mock_socket_mod.SocketModeHandler = mock_sm_handler_cls

    # slack_sdk mocks
    mock_bot_client = MagicMock()
    mock_bot_client.auth_test.return_value = {
        "user_id": "UBOTAPP001",
        "user": "lobster-bot",
    }

    mock_user_client = MagicMock()
    mock_user_client.auth_test.return_value = {
        "user_id": FAKE_SELF_USER_ID,
        "user": "lobster-user",
    }

    # WebClient returns bot_client first (for `client`), then user_client
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

    # watchdog mocks
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
        # The bot-client and user-client objects, for assertions
        "_bot_client": mock_bot_client,
        "_user_client": mock_user_client,
    }


def _minimal_env(**overrides):
    """Return a minimal valid env dict for loading slack_router."""
    base = {
        "LOBSTER_SLACK_BOT_TOKEN": "xoxb-fake-bot-token",
        "LOBSTER_SLACK_APP_TOKEN": "xapp-fake-app-token",
        "LOBSTER_SLACK_USER_TOKEN": "xoxp-fake-user-token",
        "LOBSTER_SLACK_POLL_CHANNELS": FAKE_USER_CHANNEL,
        "LOBSTER_SLACK_CHANNEL_REMAP": f"{FAKE_BOT_CHANNEL}:{FAKE_USER_CHANNEL}",
        # Prevent actual file I/O from module-level mkdir calls
        "LOBSTER_MESSAGES": "/tmp/lobster-test-messages",
        "LOBSTER_WORKSPACE": "/tmp/lobster-test-workspace",
    }
    base.update(overrides)
    return base


def _load_module(env: dict):
    """Reload slack_router with a patched environment and mocked Slack clients.

    Returns the reloaded module.  All Slack SDK imports are pre-injected into
    sys.modules so the top-level ``from slack_bolt import App`` never hits the
    real package (which is an optional dep not installed in the test venv).
    """
    # Remove cached module so module-level code reruns cleanly
    for key in list(sys.modules.keys()):
        if "slack_router" in key or key == "src.bot.slack_router":
            del sys.modules[key]

    mocks = _make_slack_mocks()
    module_patches = {
        k: v
        for k, v in mocks.items()
        if not k.startswith("_")  # skip private helper keys
    }

    # Also mock the channels.outbox dependency
    mock_outbox_mod = MagicMock()
    mock_outbox_mod.OutboxFileHandler = MagicMock()
    mock_outbox_mod.OutboxWatcher = MagicMock()
    mock_outbox_mod.drain_outbox = MagicMock()
    module_patches["channels.outbox"] = mock_outbox_mod

    # RotatingFileHandler needs a real handler-like object so logging's
    # internal level comparison (record.levelno >= hdlr.level) works.
    # Using NullHandler satisfies the interface without touching the filesystem.
    null_handler = logging.NullHandler()

    with patch.dict(sys.modules, module_patches), \
         patch.dict(os.environ, env, clear=True), \
         patch("pathlib.Path.mkdir"), \
         patch("logging.handlers.RotatingFileHandler", return_value=null_handler):
        import src.bot.slack_router as m
        # Expose the underlying client mocks on the module for test assertions
        m._test_user_client = mocks["_user_client"]
        m._test_bot_client = mocks["_bot_client"]
        return m


# ---------------------------------------------------------------------------
# 1. parse_channel_remap — pure function, no Slack dep needed
# ---------------------------------------------------------------------------

class TestParseChannelRemap:
    """LOBSTER_SLACK_CHANNEL_REMAP is parsed into a mapping dict."""

    def _import_parse(self):
        """Load the module and return parse_channel_remap."""
        m = _load_module(_minimal_env())
        return m.parse_channel_remap

    def test_single_pair_parsed(self):
        fn = self._import_parse()
        assert fn(f"{FAKE_BOT_CHANNEL}:{FAKE_USER_CHANNEL}") == {
            FAKE_BOT_CHANNEL: FAKE_USER_CHANNEL
        }

    def test_multiple_pairs_parsed(self):
        fn = self._import_parse()
        assert fn("DAAA:DBBB,DCCC:DDDD") == {"DAAA": "DBBB", "DCCC": "DDDD"}

    def test_empty_string_returns_empty_dict(self):
        fn = self._import_parse()
        assert fn("") == {}

    def test_whitespace_stripped(self):
        fn = self._import_parse()
        result = fn(f" {FAKE_BOT_CHANNEL} : {FAKE_USER_CHANNEL} ")
        assert result == {FAKE_BOT_CHANNEL: FAKE_USER_CHANNEL}

    def test_malformed_entry_skipped(self):
        """An entry without a colon is skipped; valid pairs are still parsed."""
        fn = self._import_parse()
        result = fn("DAAA:DBBB,NOCOHERE,DCCC:DDDD")
        assert result == {"DAAA": "DBBB", "DCCC": "DDDD"}

    def test_no_hardcoded_channel_ids_in_source(self):
        """Regression: the real channel IDs that were previously hardcoded must
        not appear as string literals anywhere in slack_router.py."""
        source = Path(__file__).parent.parent.parent.parent / "src" / "bot" / "slack_router.py"
        text = source.read_text()
        assert "D0B1L2Q99UN" not in text, "Hardcoded bot-DM channel ID found in source"
        assert "D0B1HPAG6NA" not in text, "Hardcoded user-DM channel ID found in source"

    def test_no_hardcoded_user_ids_in_source(self):
        """Regression: the hardcoded xoxp- user ID must not appear in source."""
        source = Path(__file__).parent.parent.parent.parent / "src" / "bot" / "slack_router.py"
        text = source.read_text()
        assert "U0B2E3TK28G" not in text, "Hardcoded self-user ID found in source"


# ---------------------------------------------------------------------------
# 2. Self-user ID resolved from auth.test at startup
# ---------------------------------------------------------------------------

class TestSelfUserIdResolution:
    """POLL_SELF_USER_ID is resolved from the xoxp- token at startup, not hardcoded."""

    def test_poll_self_user_id_matches_auth_test_response(self):
        """Module-level POLL_SELF_USER_ID equals the user_id returned by auth.test."""
        m = _load_module(_minimal_env())
        assert m.POLL_SELF_USER_ID == FAKE_SELF_USER_ID

    def test_poll_self_user_id_absent_when_no_user_token(self):
        """When LOBSTER_SLACK_USER_TOKEN is not set, POLL_SELF_USER_ID is None."""
        env = _minimal_env()
        del env["LOBSTER_SLACK_USER_TOKEN"]
        env["LOBSTER_SLACK_POLL_CHANNELS"] = ""
        m = _load_module(env)
        assert m.POLL_SELF_USER_ID is None


# ---------------------------------------------------------------------------
# 3. Startup warning for missing poll channels
# ---------------------------------------------------------------------------

class TestPollChannelsValidation:
    """When user token is set but LOBSTER_SLACK_POLL_CHANNELS is empty, warn."""

    def test_warning_logged_when_poll_channels_empty(self, caplog):
        env = _minimal_env()
        env["LOBSTER_SLACK_POLL_CHANNELS"] = ""
        env["LOBSTER_SLACK_CHANNEL_REMAP"] = ""

        with caplog.at_level(logging.WARNING, logger="lobster-slack"):
            _load_module(env)

        warning_text = caplog.text.lower()
        # Must mention poll channels being empty/disabled and what to set
        assert (
            "lobster_slack_poll_channels" in warning_text
            or "poll_channels" in warning_text
            or "poll channels" in warning_text
        ), f"Expected poll-channels warning, got: {caplog.text}"

    def test_no_warning_when_poll_channels_set(self, caplog):
        env = _minimal_env()
        with caplog.at_level(logging.WARNING, logger="lobster-slack"):
            _load_module(env)

        # Only failure warnings allowed — no poll-channels-empty warning
        for record in caplog.records:
            if record.levelno >= logging.WARNING:
                assert "poll_channels" not in record.message.lower() or \
                       "empty" not in record.message.lower(), \
                    f"Unexpected poll-channels warning: {record.message}"


# ---------------------------------------------------------------------------
# 4. Inbound channel remap uses config-driven dict
# ---------------------------------------------------------------------------

class TestInboundChannelRemap:
    """Inbound messages on the remapped channel are stored with the destination channel_id."""

    def test_inbound_remap_uses_channel_remap_config(self):
        """The CHANNEL_REMAP module-level dict is populated from the env var."""
        m = _load_module(_minimal_env())
        assert m.CHANNEL_REMAP == {FAKE_BOT_CHANNEL: FAKE_USER_CHANNEL}

    def test_inbound_remap_empty_when_env_var_not_set(self):
        """When LOBSTER_SLACK_CHANNEL_REMAP is not set, CHANNEL_REMAP is empty."""
        env = _minimal_env()
        del env["LOBSTER_SLACK_CHANNEL_REMAP"]
        m = _load_module(env)
        assert m.CHANNEL_REMAP == {}

    def test_inbound_source_channel_remapped_to_destination(self, monkeypatch):
        """A message on the source channel is written to inbox with the destination chat_id."""
        m = _load_module(_minimal_env())

        written = {}

        def fake_write(msg_data):
            written.update(msg_data)

        monkeypatch.setattr(m, "write_message_to_inbox", fake_write)
        monkeypatch.setattr(m, "get_user_info", lambda uid: {
            "name": "testuser", "profile": {}, "real_name": "Test User"
        })
        monkeypatch.setattr(m, "get_channel_info", lambda cid: {
            "name": "dm", "is_im": True
        })
        monkeypatch.setattr(m, "_channel_config", None)
        monkeypatch.setattr(m, "_CHANNEL_CONFIG_ENABLED", False)
        monkeypatch.setattr(m, "_INGRESS_LOGGING_ENABLED", False)
        monkeypatch.setattr(m, "ALLOWED_CHANNELS", [])
        monkeypatch.setattr(m, "ALLOWED_USERS", [])

        # Simulate a Socket Mode event on the source (bot-DM) channel
        body = {
            "event": {
                "user": "USENDER001",
                "channel": FAKE_BOT_CHANNEL,
                "text": "hello",
                "ts": "1234567890.000100",
                "thread_ts": None,
            }
        }

        m.handle_message_events(body=body, say=MagicMock(), logger=MagicMock())

        assert written.get("chat_id") == FAKE_USER_CHANNEL, (
            f"Expected chat_id={FAKE_USER_CHANNEL!r}, got {written.get('chat_id')!r}"
        )


# ---------------------------------------------------------------------------
# 5. Outbound channel remap uses config-driven dict
# ---------------------------------------------------------------------------

class TestOutboundChannelRemap:
    """Outbound replies to the source channel are sent to the destination channel."""

    def test_outbound_source_channel_remapped_to_destination(self):
        """_send_slack_reply remaps the source channel to the destination before posting."""
        m = _load_module(_minimal_env())

        # user_client is the module-level client used for outbound sends
        m.user_client.chat_postMessage.reset_mock()

        reply = {
            "chat_id": FAKE_BOT_CHANNEL,
            "text": "hello back",
            "source": "slack",
        }

        m._send_slack_reply(reply)

        posted_channel = m.user_client.chat_postMessage.call_args[1]["channel"]
        assert posted_channel == FAKE_USER_CHANNEL, (
            f"Expected outbound channel={FAKE_USER_CHANNEL!r}, got {posted_channel!r}"
        )

    def test_outbound_destination_channel_sent_unchanged(self):
        """Outbound replies already targeting the destination channel are sent as-is."""
        m = _load_module(_minimal_env())
        m.user_client.chat_postMessage.reset_mock()

        reply = {
            "chat_id": FAKE_USER_CHANNEL,
            "text": "hello back",
            "source": "slack",
        }

        m._send_slack_reply(reply)

        posted_channel = m.user_client.chat_postMessage.call_args[1]["channel"]
        assert posted_channel == FAKE_USER_CHANNEL

    def test_outbound_unknown_channel_sent_unchanged(self):
        """Outbound replies to an unmapped channel are sent as-is."""
        m = _load_module(_minimal_env())
        m.user_client.chat_postMessage.reset_mock()

        reply = {
            "chat_id": "DUNKNOWN001",
            "text": "hi",
            "source": "slack",
        }

        m._send_slack_reply(reply)

        posted_channel = m.user_client.chat_postMessage.call_args[1]["channel"]
        assert posted_channel == "DUNKNOWN001"


# ---------------------------------------------------------------------------
# 6. _remap_channel helper (PR 2)
# ---------------------------------------------------------------------------

class TestRemapChannelHelper:
    """_remap_channel is the single shared lookup for both inbound and outbound paths."""

    def test_remap_channel_maps_src_to_dst(self):
        """Source channel is mapped to its destination."""
        m = _load_module(_minimal_env())
        assert m._remap_channel(FAKE_BOT_CHANNEL) == FAKE_USER_CHANNEL

    def test_remap_channel_returns_unchanged_when_not_in_map(self):
        """An unknown channel is returned unchanged."""
        m = _load_module(_minimal_env())
        assert m._remap_channel("DUNKNOWN999") == "DUNKNOWN999"

    def test_remap_channel_returns_unchanged_when_map_empty(self):
        """When CHANNEL_REMAP is empty, all channels are returned unchanged."""
        env = _minimal_env()
        del env["LOBSTER_SLACK_CHANNEL_REMAP"]
        m = _load_module(env)
        assert m._remap_channel(FAKE_BOT_CHANNEL) == FAKE_BOT_CHANNEL

    def test_inbound_and_outbound_use_same_remap_table(self):
        """Both inbound handler and outbound send function call _remap_channel.

        Verified by monkey-patching _remap_channel and confirming both paths
        reach it.  This is the key invariant: the single helper eliminates
        any risk of the two call sites drifting out of sync.
        """
        m = _load_module(_minimal_env())

        calls = []

        def tracking_remap(channel_id):
            calls.append(("remap_called", channel_id))
            return m.CHANNEL_REMAP.get(channel_id, channel_id)

        # Patch the helper on the module
        import types
        m._remap_channel = tracking_remap

        # Trigger inbound path
        written = {}

        def fake_write(msg_data):
            written.update(msg_data)

        m.write_message_to_inbox = fake_write
        m.get_user_info = lambda uid: {"name": "u", "profile": {}, "real_name": "U"}
        m.get_channel_info = lambda cid: {"name": "dm", "is_im": True}
        m._channel_config = None
        m._CHANNEL_CONFIG_ENABLED = False
        m._INGRESS_LOGGING_ENABLED = False
        m.ALLOWED_CHANNELS = []
        m.ALLOWED_USERS = []

        body = {
            "event": {
                "user": "USENDER001",
                "channel": FAKE_BOT_CHANNEL,
                "text": "hello",
                "ts": "1234567890.000200",
                "thread_ts": None,
            }
        }
        m.handle_message_events(body=body, say=MagicMock(), logger=MagicMock())

        # Trigger outbound path
        m.user_client.chat_postMessage.reset_mock()
        reply = {"chat_id": FAKE_BOT_CHANNEL, "text": "reply", "source": "slack"}
        m._send_slack_reply(reply)

        # Both paths must have called our tracking remap
        call_channels = [c for _, c in calls]
        assert FAKE_BOT_CHANNEL in call_channels, (
            f"_remap_channel not called with {FAKE_BOT_CHANNEL!r}; calls={calls}"
        )
        assert len(calls) >= 2, f"Expected at least 2 remap calls, got {len(calls)}"
