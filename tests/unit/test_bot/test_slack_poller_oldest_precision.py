"""
Regression tests for a silent-drop bug in the Slack channel/DM pollers'
``oldest`` checkpoint bump.

Incident: Jake sent a message in #wallace-jake (channel C0B3S6RBFV5) that was
never delivered, even though the poller's heartbeat looked perfectly healthy
(fresh timestamps every tick, no errors, no rate-limit warnings). Ground truth
from Slack's own `conversations.history` showed the message was really there
and really unanswered.

Root cause: both `_poll_one_channel()` and `_poll_user_dm_channels()` compute
the next poll's exclusive-lower-bound `oldest` parameter as::

    str(float(oldest) + 0.000001)

Slack timestamps are always ``<10-digit-seconds>.<6-digit-microseconds>`` —
already 16 significant decimal digits, right at the edge of what a 64-bit
float can represent exactly. Adding a small epsilon and `str()`-ing the result
can silently produce *7* decimal digits (e.g. checkpoint
``"1784568407.379109"`` yields ``"1784568407.3791099"``). Slack's API does not
reject this malformed value — it misparses it (observed: the decimal point
shifts, producing an `oldest` far in the future) and returns
``{"ok": true, "messages": []}``. No exception, no rate-limit signal, nothing
to log. The channel's on-disk checkpoint then never advances again, and every
future message on that channel is dropped forever — even though the poller's
heartbeat (which only proves the *attempt* happened, not that anything useful
came back) looks completely healthy.

The fix: format the bumped timestamp with a fixed 6 decimal places
(`f"{...:.6f}"`), which always matches Slack's own timestamp format regardless
of the input's exact bit pattern.
"""

import sys
from pathlib import Path

# Ensure src is importable
_SRC = Path(__file__).parent.parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Real checkpoint values pulled from the production incident (2026-07-20).
# Roughly half of real Slack timestamps trigger the bug — it depends on the
# exact bit pattern of the microsecond value, not anything special about this
# one channel.
# ---------------------------------------------------------------------------
KNOWN_BUGGY_CHECKPOINTS = [
    "1784568407.379109",  # Jake's channel (C0B3S6RBFV5) — the actual incident
    "1784278808.621729",  # a second channel that would have hit this too
]
KNOWN_SAFE_CHECKPOINTS = [
    "1784451627.538489",
    "1784569292.513319",
    "1784569289.249039",
]


def _old_buggy_bump(ts_str: str) -> str:
    """The exact pre-fix expression, isolated for comparison in tests."""
    return str(float(ts_str) + 0.000001)


class TestOldBumpWasBuggy:
    """Document the bug in the old expression so a future refactor can't
    silently reintroduce it without a test noticing."""

    def test_old_expression_produces_more_than_6_decimal_places(self):
        offenders = [
            ts for ts in KNOWN_BUGGY_CHECKPOINTS
            if len(_old_buggy_bump(ts).split(".")[1]) != 6
        ]
        assert offenders, (
            "Expected at least one known-buggy checkpoint to reproduce the "
            "7-decimal-place corruption with the old expression"
        )

    def test_malformed_value_is_rejected_by_a_strict_slack_ts_parser(self):
        """Simulates Slack's own handling: a well-formed Slack ts always has
        exactly 6 digits after the decimal point. A value with 7 digits is
        the exact shape that got misparsed in production (observed: Slack
        echoed back an `oldest` with the decimal point shifted, excluding
        every real message)."""
        buggy = _old_buggy_bump("1784568407.379109")
        decimals = buggy.split(".")[1]
        assert len(decimals) == 7, (
            "This test documents the exact corruption seen in production; "
            f"got {buggy!r}"
        )


class TestNextOldestBoundFix:
    """`_next_oldest_bound()` must always yield a well-formed 6-decimal-place
    Slack timestamp, regardless of the input's floating-point bit pattern."""

    def _load(self):
        import bot.slack_router as sr
        return sr

    def test_always_exactly_six_decimal_places(self):
        m = self._load()
        for ts in KNOWN_BUGGY_CHECKPOINTS + KNOWN_SAFE_CHECKPOINTS:
            result = m._next_oldest_bound(ts)
            decimals = result.split(".")[1]
            assert len(decimals) == 6, (
                f"_next_oldest_bound({ts!r}) = {result!r} has "
                f"{len(decimals)} decimal places, expected 6"
            )

    def test_result_is_strictly_greater_than_input(self):
        """The bound must still be a real exclusive lower bound — i.e. not
        rounded down to equal (or below) the original checkpoint, which would
        cause the already-processed message to be redelivered forever."""
        m = self._load()
        for ts in KNOWN_BUGGY_CHECKPOINTS + KNOWN_SAFE_CHECKPOINTS:
            result = m._next_oldest_bound(ts)
            assert float(result) > float(ts), (
                f"_next_oldest_bound({ts!r}) = {result!r} did not advance "
                "past the input"
            )

    def test_matches_the_known_good_manually_computed_value(self):
        """Regression pin for the exact incident checkpoint: the poller must
        compute the same bound that was independently verified (via a direct
        Slack API call in the live incident investigation) to correctly
        return the previously-missed message."""
        m = self._load()
        assert m._next_oldest_bound("1784568407.379109") == "1784568407.379110"


class TestPollOneChannelUsesFixedBound:
    """End-to-end: `_poll_one_channel()` must pass a well-formed `oldest` to
    `conversations.history`, or Slack silently returns nothing and the
    channel is wedged forever — exactly the production incident."""

    def _load_module(self, tmp_path):
        import importlib
        import os
        from unittest.mock import MagicMock, patch

        for key in list(sys.modules.keys()):
            if "slack_router" in key:
                del sys.modules[key]

        mock_app = MagicMock()
        mock_app.event.side_effect = lambda *a, **kw: (lambda fn: fn)
        mock_bolt_mod = MagicMock()
        mock_bolt_mod.App = MagicMock(return_value=mock_app)

        mock_socket_mod = MagicMock()

        mock_bot_client = MagicMock()
        mock_bot_client.auth_test.return_value = {"user_id": "UBOTAPP001", "user": "bot"}
        mock_user_client = MagicMock()
        mock_user_client.auth_test.return_value = {"user_id": "USELF00001", "user": "self"}
        mock_user_client.conversations_history.return_value = {"messages": []}

        call_count = [0]

        def webclient_side_effect(token=None, **kw):
            call_count[0] += 1
            return mock_bot_client if call_count[0] == 1 else mock_user_client

        mock_sdk_mod = MagicMock()
        mock_sdk_mod.WebClient = MagicMock(side_effect=webclient_side_effect)

        class _FakeSlackApiError(Exception):
            def __init__(self, message="error", response=None):
                super().__init__(message)
                self.response = response or {}

        mock_errors_mod = MagicMock()
        mock_errors_mod.SlackApiError = _FakeSlackApiError

        mock_outbox_mod = MagicMock()
        mock_outbox_mod.OutboxFileHandler = MagicMock()
        mock_outbox_mod.OutboxWatcher = MagicMock()
        mock_outbox_mod.drain_outbox = MagicMock()

        env = {
            "LOBSTER_SLACK_BOT_TOKEN": "xoxb-fake",
            "LOBSTER_SLACK_APP_TOKEN": "xapp-fake",
            "LOBSTER_SLACK_USER_TOKEN": "xoxp-fake",
            "LOBSTER_SLACK_CHANNEL_CONVERSATIONS": "CINCIDENT01",
            "LOBSTER_SLACK_POLL_CHANNELS": "",
            "LOBSTER_MESSAGES": str(tmp_path / "messages"),
            "LOBSTER_WORKSPACE": str(tmp_path / "workspace"),
        }

        module_patches = {
            "slack_bolt": mock_bolt_mod,
            "slack_bolt.adapter": MagicMock(),
            "slack_bolt.adapter.socket_mode": mock_socket_mod,
            "slack_sdk": mock_sdk_mod,
            "slack_sdk.errors": mock_errors_mod,
            "watchdog": MagicMock(),
            "watchdog.observers": MagicMock(),
            "watchdog.events": MagicMock(),
            "channels.outbox": mock_outbox_mod,
        }

        import logging
        null_handler = logging.NullHandler()

        with patch.dict(sys.modules, module_patches), \
             patch.dict(os.environ, env, clear=True), \
             patch("pathlib.Path.mkdir"), \
             patch("logging.handlers.RotatingFileHandler", return_value=null_handler):
            import src.bot.slack_router as m
            m.SlackApiError = mock_errors_mod.SlackApiError
            return m, mock_user_client

    def test_conversations_history_called_with_six_decimal_oldest(self, tmp_path):
        m, client = self._load_module(tmp_path)
        m._channel_poll_client = client
        m._channel_poll_state = {"conv_CINCIDENT01": "1784568407.379109"}
        m._channel_skip_ids = set()
        m._channel_backoff = {}

        m._poll_one_channel("CINCIDENT01")

        assert client.conversations_history.called
        oldest_used = client.conversations_history.call_args.kwargs["oldest"]
        decimals = oldest_used.split(".")[1]
        assert len(decimals) == 6, (
            f"_poll_one_channel passed oldest={oldest_used!r} to "
            "conversations.history — a malformed (non-6-decimal) value is "
            "exactly what caused Slack to silently return zero messages in "
            "the production incident"
        )
        assert oldest_used == "1784568407.379110"
