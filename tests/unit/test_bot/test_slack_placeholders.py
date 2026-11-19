"""
Tests for reply -> "..." placeholder matching in the Slack router (issue #2325).

Background: every inbound Slack message gets a "..." placeholder.  The old
router kept a per-channel FIFO and every outbound reply popped the *oldest*
placeholder.  The dispatcher routinely answers a burst of N messages with ONE
batched reply, so N-1 placeholders were orphaned and every later reply edited
a stale message far up the channel.

Required behaviour (from the issue):
- A reply resolves the NEWEST pending placeholder in its conversation; older
  placeholders from the same burst are deleted as superseded.
- An explicit reply target (``reply_to_slack_ts``, or a ``thread_ts`` equal to
  a top-level message's ts) wins over newest-first.
- Channels (and threads, when the reply is threaded) are isolated.
- Placeholders older than PLACEHOLDER_MAX_AGE_SECONDS expire and are deleted.
- Pending placeholders survive a router restart (persisted), and expired ones
  are cleaned up on load.
- A reply with no pending placeholder is posted as a new message.
"""

import importlib
import itertools
import os
from unittest.mock import MagicMock, patch

import pytest

from src.bot.slack_placeholders import (
    DEFAULT_PLACEHOLDER_MAX_AGE_SECONDS,
    Placeholder,
    PlaceholderStore,
    partition_expired,
    resolve_reply,
)

BURST_SIZE = 5
MAX_AGE = DEFAULT_PLACEHOLDER_MAX_AGE_SECONDS
CHANNEL_A = "C0CHANA0001"
CHANNEL_B = "C0CHANB0001"


def ph(n: int, channel: str = CHANNEL_A, thread_ts: str | None = None, created_at: float | None = None) -> Placeholder:
    """Build a placeholder for the n-th inbound message."""
    return Placeholder(
        channel_id=channel,
        thread_ts=thread_ts,
        slack_ts=f"1000.{n:06d}",
        placeholder_ts=f"2000.{n:06d}",
        created_at=float(n) if created_at is None else created_at,
    )


# ---------------------------------------------------------------------------
# Pure resolution logic
# ---------------------------------------------------------------------------

class TestResolveReply:
    def test_burst_reply_targets_newest_and_supersedes_the_rest(self):
        pending = tuple(ph(i) for i in range(1, BURST_SIZE + 1))
        r = resolve_reply(pending, CHANNEL_A, None, now=BURST_SIZE + 1, max_age=MAX_AGE)
        assert r.target == ph(BURST_SIZE)
        assert set(r.stale) == {ph(i) for i in range(1, BURST_SIZE)}
        assert r.remaining == ()

    def test_no_placeholder_returns_no_target(self):
        r = resolve_reply((), CHANNEL_A, None, now=1, max_age=MAX_AGE)
        assert r.target is None and r.stale == () and r.remaining == ()

    def test_other_channels_are_untouched(self):
        pending = (ph(1, CHANNEL_A), ph(2, CHANNEL_B), ph(3, CHANNEL_A))
        r = resolve_reply(pending, CHANNEL_A, None, now=4, max_age=MAX_AGE)
        assert r.target == ph(3, CHANNEL_A)
        assert r.stale == (ph(1, CHANNEL_A),)
        assert r.remaining == (ph(2, CHANNEL_B),)

    def test_expired_placeholders_are_never_targets(self):
        old = ph(1, created_at=0.0)
        r = resolve_reply((old,), CHANNEL_A, None, now=MAX_AGE + 1, max_age=MAX_AGE)
        assert r.target is None
        assert r.stale == (old,)

    def test_explicit_reply_to_targets_that_message_and_keeps_newer(self):
        pending = tuple(ph(i) for i in range(1, 4))
        r = resolve_reply(
            pending, CHANNEL_A, None, now=5, max_age=MAX_AGE,
            reply_to_slack_ts=ph(2).slack_ts,
        )
        assert r.target == ph(2)
        assert r.stale == (ph(1),)
        assert r.remaining == (ph(3),)

    def test_threaded_reply_only_considers_that_thread(self):
        t = "1000.999999"
        pending = (ph(1), ph(2, thread_ts=t), ph(3))
        r = resolve_reply(pending, CHANNEL_A, t, now=4, max_age=MAX_AGE)
        assert r.target == ph(2, thread_ts=t)
        assert r.stale == ()
        assert set(r.remaining) == {ph(1), ph(3)}

    def test_thread_ts_equal_to_top_level_message_targets_that_message(self):
        # Dispatcher threads its reply under the user's top-level message.
        pending = (ph(1), ph(2), ph(3))
        r = resolve_reply(pending, CHANNEL_A, ph(2).slack_ts, now=4, max_age=MAX_AGE)
        assert r.target == ph(2)
        assert r.stale == (ph(1),)
        assert r.remaining == (ph(3),)

    def test_unthreaded_reply_matches_threaded_placeholder(self):
        # The dispatcher usually omits thread_ts even when the user wrote in a thread.
        t = "1000.999999"
        r = resolve_reply((ph(1, thread_ts=t),), CHANNEL_A, None, now=2, max_age=MAX_AGE)
        assert r.target == ph(1, thread_ts=t)

    def test_unrelated_thread_reply_leaves_channel_placeholders_alone(self):
        pending = (ph(1), ph(2))
        r = resolve_reply(pending, CHANNEL_A, "1000.555555", now=3, max_age=MAX_AGE)
        assert r.target is None
        assert r.stale == ()
        assert r.remaining == pending


class TestPartitionExpired:
    def test_splits_by_age(self):
        fresh, old = ph(1, created_at=100.0), ph(2, created_at=0.0)
        f, e = partition_expired((fresh, old), now=MAX_AGE + 50, max_age=MAX_AGE)
        assert f == (fresh,) and e == (old,)


# ---------------------------------------------------------------------------
# Store: locking + persistence
# ---------------------------------------------------------------------------

class TestPlaceholderStore:
    def test_persists_and_restores_across_restart(self, tmp_path):
        state = tmp_path / "ph.json"
        s1 =PlaceholderStore(state, clock=lambda: 10.0, max_age=MAX_AGE)
        s1.add(ph(1, created_at=10.0))
        s1.add(ph(2, CHANNEL_B, created_at=10.0))

        s2 = PlaceholderStore(state, clock=lambda: 20.0, max_age=MAX_AGE)
        expired = s2.load()
        assert expired == ()
        assert set(s2.pending()) == {ph(1, created_at=10.0), ph(2, CHANNEL_B, created_at=10.0)}

    def test_load_returns_expired_entries_for_cleanup(self, tmp_path):
        state = tmp_path / "ph.json"
        PlaceholderStore(state, clock=lambda: 0.0, max_age=MAX_AGE).add(ph(1, created_at=0.0))
        s2 = PlaceholderStore(state, clock=lambda: MAX_AGE + 1.0, max_age=MAX_AGE)
        assert s2.load() == (ph(1, created_at=0.0),)
        assert s2.pending() == ()
        # Cleanup is persisted: a third start sees nothing.
        s3 = PlaceholderStore(state, clock=lambda: MAX_AGE + 2.0, max_age=MAX_AGE)
        assert s3.load() == () and s3.pending() == ()

    def test_corrupt_state_file_is_ignored(self, tmp_path):
        state = tmp_path / "ph.json"
        state.write_text("{not json")
        s = PlaceholderStore(state, clock=lambda: 0.0, max_age=MAX_AGE)
        assert s.load() == () and s.pending() == ()

    def test_claim_persists_removal(self, tmp_path):
        state = tmp_path / "ph.json"
        s = PlaceholderStore(state, clock=lambda: 3.0, max_age=MAX_AGE)
        s.add(ph(1))
        s.add(ph(2))
        r = s.claim(CHANNEL_A, None)
        assert r.target == ph(2)
        s2 = PlaceholderStore(state, clock=lambda: 3.0, max_age=MAX_AGE)
        s2.load()
        assert s2.pending() == ()

    def test_sweep_removes_only_expired(self, tmp_path):
        now = {"t": 0.0}
        s = PlaceholderStore(tmp_path / "ph.json", clock=lambda: now["t"], max_age=MAX_AGE)
        s.add(ph(1, created_at=0.0))
        s.add(ph(2, created_at=MAX_AGE))
        now["t"] = MAX_AGE + 1
        assert s.sweep() == (ph(1, created_at=0.0),)
        assert s.pending() == (ph(2, created_at=MAX_AGE),)


# ---------------------------------------------------------------------------
# End-to-end through slack_router with a mocked Slack client
# ---------------------------------------------------------------------------

def load_router():
    env = {
        "LOBSTER_SLACK_BOT_TOKEN": "xoxb-test",
        "LOBSTER_SLACK_APP_TOKEN": "xapp-test",
        "LOBSTER_SLACK_USER_TOKEN": "xoxp-test",
        "LOBSTER_SLACK_CHANNEL_REMAP": "",
        "LOBSTER_SLACK_TYPING_INDICATOR": "true",
    }
    with patch.dict(os.environ, env):
        with patch("slack_bolt.App") as app_cls, \
             patch("slack_sdk.WebClient") as client_cls, \
             patch("slack_bolt.adapter.socket_mode.SocketModeHandler"):
            app_cls.return_value = MagicMock()
            wc = MagicMock()
            wc.auth_test.return_value = {"user_id": "U0BOT", "user": "bot"}
            client_cls.return_value = wc
            import src.bot.slack_router as router
            importlib.reload(router)
            return router


class FakeSlack:
    """Minimal in-memory Slack: tracks every message's text by (channel, ts)."""

    def __init__(self):
        self.messages: dict[tuple[str, str], str] = {}
        self._ts = itertools.count(1)

    def chat_postMessage(self, channel, text, thread_ts=None, **_):
        ts = f"9000.{next(self._ts):06d}"
        self.messages[(channel, ts)] = text
        return {"ts": ts}

    def chat_update(self, channel, ts, text, **_):
        self.messages[(channel, ts)] = text
        return {"ok": True}

    def chat_delete(self, channel, ts, **_):
        self.messages.pop((channel, ts), None)
        return {"ok": True}

    def in_channel(self, channel):
        return [t for (c, ts), t in sorted(self.messages.items()) if c == channel]


@pytest.fixture
def router(tmp_path):
    r = load_router()
    fake = FakeSlack()
    r.user_client = fake
    now = {"t": 1000.0}
    r._placeholders = PlaceholderStore(tmp_path / "ph.json", clock=lambda: now["t"], max_age=MAX_AGE)
    r._fake, r._now = fake, now
    yield r


def inbound(router, channel, n):
    return router._post_typing_placeholder(channel, None, f"1000.{n:06d}")


def reply(router, channel, text, **extra):
    return router._send_slack_reply({"chat_id": channel, "text": text, **extra})


class TestRouterEndToEnd:
    def test_burst_then_single_message_updates_its_own_placeholder(self, router):
        fake = router._fake
        for i in range(BURST_SIZE):
            inbound(router, CHANNEL_A, i)
        assert reply(router, CHANNEL_A, "batched answer")
        # Exactly one message remains: the batched answer. No "..." orphans.
        assert fake.in_channel(CHANNEL_A) == ["batched answer"]

        ph_ts = inbound(router, CHANNEL_A, 99)
        assert reply(router, CHANNEL_A, "answer to 99")
        assert fake.messages[(CHANNEL_A, ph_ts)] == "answer to 99"
        assert fake.in_channel(CHANNEL_A) == ["batched answer", "answer to 99"]

    def test_multi_channel_isolation(self, router):
        fake = router._fake
        chan_a = inbound(router, CHANNEL_A, 1)
        chan_b = inbound(router, CHANNEL_B, 2)
        reply(router, CHANNEL_B, "hi B")
        assert fake.messages[(CHANNEL_B, chan_b)] == "hi B"
        assert fake.messages[(CHANNEL_A, chan_a)] == "..."
        reply(router, CHANNEL_A, "hi A")
        assert fake.messages[(CHANNEL_A, chan_a)] == "hi A"

    def test_stale_placeholder_expires_and_reply_posts_new_message(self, router):
        fake = router._fake
        old = inbound(router, CHANNEL_A, 1)
        router._now["t"] += MAX_AGE + 1
        assert reply(router, CHANNEL_A, "late reply")
        assert (CHANNEL_A, old) not in fake.messages
        assert fake.in_channel(CHANNEL_A) == ["late reply"]

    def test_periodic_sweep_deletes_expired_placeholders(self, router):
        fake = router._fake
        inbound(router, CHANNEL_A, 1)
        router._now["t"] += MAX_AGE + 1
        router._sweep_expired_placeholders()
        assert fake.in_channel(CHANNEL_A) == []

    def test_reply_with_no_placeholder_posts_normally(self, router):
        fake = router._fake
        assert reply(router, CHANNEL_A, "proactive")
        assert fake.in_channel(CHANNEL_A) == ["proactive"]

    def test_restart_restores_pending_placeholder(self, router, tmp_path):
        fake = router._fake
        ph_ts = inbound(router, CHANNEL_A, 1)
        # Simulate a restart: fresh store over the same state file.
        router._placeholders = PlaceholderStore(
            tmp_path / "ph.json", clock=lambda: router._now["t"], max_age=MAX_AGE
        )
        router._restore_placeholders_on_startup()
        reply(router, CHANNEL_A, "after restart")
        assert fake.messages[(CHANNEL_A, ph_ts)] == "after restart"

    def test_restart_deletes_expired_placeholders(self, router, tmp_path):
        fake = router._fake
        inbound(router, CHANNEL_A, 1)
        router._now["t"] += MAX_AGE + 1
        router._placeholders = PlaceholderStore(
            tmp_path / "ph.json", clock=lambda: router._now["t"], max_age=MAX_AGE
        )
        router._restore_placeholders_on_startup()
        assert fake.in_channel(CHANNEL_A) == []

    def test_delete_failure_does_not_block_reply(self, router):
        fake = router._fake
        inbound(router, CHANNEL_A, 1)
        target = inbound(router, CHANNEL_A, 2)
        from slack_sdk.errors import SlackApiError

        def boom(**_):
            raise SlackApiError("nope", MagicMock(data={"error": "message_not_found"}))

        fake.chat_delete = boom
        assert reply(router, CHANNEL_A, "answer")
        assert fake.messages[(CHANNEL_A, target)] == "answer"
