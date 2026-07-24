"""
Tests for the BIS-744 shared post-push confirmation helper:
``_queue_push_confirmation`` and its supporting functions
(``_confirmation_already_sent``, ``_write_system_alert``) in
``src/mcp/inbox_server_http.py``.

BIS-743 copy-pasted an identical confirmation block into all three push
endpoints (calendar, gmail, workspace). BIS-744 extracts the shared logic
into ``_queue_push_confirmation`` and upgrades it with:

1. A live-data preview (via a per-scope ``fetch_preview`` callable).
2. Failure visibility: the fetch and the outbox write are each wrapped so a
   failure is (a) logged at ERROR with exc_info, never silently swallowed at
   WARNING with no fallback, and (b) still produces *some* user-facing
   confirmation when only the preview fetch failed, or a system-level alert
   (chat_id=0, written to the inbox) when the notify step itself failed and
   no user-facing message could be delivered at all.
3. A de-dupe guard keyed on (scope, chat_id) with a TTL window.

These tests exercise ``_queue_push_confirmation`` directly (not through the
HTTP layer) so the core logic is pinned once, cleanly, independent of any
particular endpoint's wiring. Endpoint-level wiring (that each endpoint calls
this helper with the right scope/text/fetch function) is covered by
test_push_{calendar,gmail,workspace}_token_confirmation.py.
"""

from __future__ import annotations

import json
import logging
import os
import time as time_module
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("MCP_HTTP_TOKEN", "test-mcp-token-placeholder")

_MODULE = "src.mcp.inbox_server_http"


@pytest.fixture()
def isolated_dirs(tmp_path):
    """Patch _MESSAGES_DIR/_INBOX_DIR and reset the dedupe/nonce state."""
    messages_base = tmp_path / "shared_confirm_messages"
    outbox_dir = messages_base / "outbox"
    inbox_dir = messages_base / "inbox"
    outbox_dir.mkdir(parents=True)
    inbox_dir.mkdir(parents=True)

    with (
        patch(f"{_MODULE}._MESSAGES_DIR", messages_base),
        patch(f"{_MODULE}._INBOX_DIR", inbox_dir),
        patch(f"{_MODULE}._seen_push_confirmations", {}),
    ):
        yield outbox_dir, inbox_dir


def _import_target():
    import src.mcp.inbox_server_http as m
    return m


class TestLiveDataPreview:
    def test_preview_included_in_confirmation_text(self, isolated_dirs):
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="111",
            scope="calendar",
            connected_text="Google Calendar connected.",
            fetch_preview=lambda: "Next up: Standup — Mon 9am",
        )

        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 1
        reply = json.loads(files[0].read_text())
        assert "Standup" in reply["text"]
        assert "Google Calendar connected." in reply["text"]

    def test_none_preview_shows_honest_degraded_message(self, isolated_dirs):
        """fetch_preview returning None/"" (nothing to show, no error) still
        produces an honest message, distinct from a hard failure."""
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="112",
            scope="calendar",
            connected_text="Google Calendar connected.",
            fetch_preview=lambda: None,
        )

        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 1
        reply = json.loads(files[0].read_text())
        assert "couldn't fetch a live preview" in reply["text"]


class TestFailureVisibility:
    def test_preview_fetch_exception_logged_at_error_not_swallowed(self, isolated_dirs, caplog):
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        def _raise():
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR):
            m._queue_push_confirmation(
                chat_id="113",
                scope="gmail",
                connected_text="Gmail connected.",
                fetch_preview=_raise,
            )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "Preview-fetch exception must be logged at ERROR"
        assert any("preview fetch failed" in r.getMessage().lower() for r in error_records)

        # And the confirmation must STILL be queued (never silent).
        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 1
        reply = json.loads(files[0].read_text())
        assert "couldn't fetch a live preview" in reply["text"]

    def test_notify_failure_logged_at_error_with_system_alert_fallback(self, isolated_dirs):
        """The pre-existing bug this fixes: a bare `except Exception:
        log.warning(...)` around the outbox write, with zero fallback. Now:
        ERROR log + a system-alert (both an inbox audit record and, when
        possible, a real admin-facing outbox message)."""
        outbox_dir, inbox_dir = isolated_dirs
        m = _import_target()

        # Force the outbox mkdir to fail by making its parent unwritable via
        # a path collision: replace the "outbox" segment with a pre-existing
        # file so mkdir(exist_ok=True) raises FileExistsError (not a dir).
        # This blocks BOTH the original confirmation write AND the admin
        # outbox-alert fallback (same outbox dir) -- the inbox audit record
        # (a separate directory) must still get through regardless.
        (outbox_dir.parent / "outbox").rmdir()
        (outbox_dir.parent / "outbox").write_text("blocking file, not a directory")

        logger = logging.getLogger(_MODULE)
        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Handler(level=logging.ERROR)
        logger.addHandler(handler)
        try:
            m._queue_push_confirmation(
                chat_id="114",
                scope="workspace",
                connected_text="Google Workspace connected.",
                fetch_preview=lambda: "some preview",
            )
        finally:
            logger.removeHandler(handler)

        error_records = [r for r in records if r.levelno >= logging.ERROR]
        assert error_records, "Notify-path failure must be logged at ERROR"
        assert any(
            "failed to queue outbox confirmation" in r.getMessage().lower()
            for r in error_records
        )

        alert_files = list(inbox_dir.glob("*_confirm_failure_alert.json"))
        assert alert_files, "System-alert inbox audit record must be written when notify fails"
        alert = json.loads(alert_files[0].read_text())
        assert alert["chat_id"] == 0
        assert alert["type"] == "system_alert"
        assert "workspace" in alert["text"].lower()
        assert "114" in alert["text"]

    def test_system_alert_delivers_real_outbox_message_to_admin_when_configured(self, isolated_dirs):
        """The actual fix for the Fable-review finding that the chat_id=0
        inbox alert alone is inert (nothing in this codebase consumes
        type="system_alert" at chat_id=0 -- inbox_server.py explicitly
        excludes chat_id==0 from USER_FACING_TYPES handling). This proves
        the real fallback: when LOBSTER_ADMIN_CHAT_ID is configured and the
        outbox itself is writable, a REAL outbox message addressed to the
        admin is produced -- the same delivery mechanism already proven to
        reach a real Telegram user."""
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        with patch.dict(os.environ, {"LOBSTER_ADMIN_CHAT_ID": "999000111"}):
            m._write_system_alert("something went wrong for scope=workspace chat_id=114")

        outbox_files = list(outbox_dir.glob("*_confirm_failure_admin_alert.json"))
        assert outbox_files, "Admin-facing outbox alert was not written"
        alert = json.loads(outbox_files[0].read_text())
        assert alert["chat_id"] == "999000111"
        assert alert["source"] == "telegram"
        assert "went wrong" in alert["text"]

    def test_system_alert_logs_loudly_when_admin_chat_id_not_configured(self, isolated_dirs, caplog):
        """If LOBSTER_ADMIN_CHAT_ID isn't set, there is genuinely no channel
        to notify a human via -- this must still be loud (ERROR), not a
        silent no-op."""
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOBSTER_ADMIN_CHAT_ID", None)
            with caplog.at_level(logging.ERROR):
                m._write_system_alert("failure with no admin channel configured")

        assert any(
            "lobster_admin_chat_id not configured" in r.getMessage().lower()
            for r in caplog.records
        )
        # No admin-outbox alert should exist when there's no chat_id to send to.
        assert not list(outbox_dir.glob("*_confirm_failure_admin_alert.json"))

    def test_system_alert_itself_failing_does_not_raise(self, isolated_dirs):
        """Last-resort: even if writing the system alert fails, the caller
        must not see an exception propagate."""
        outbox_dir, inbox_dir = isolated_dirs
        m = _import_target()

        (outbox_dir.parent / "outbox").rmdir()
        (outbox_dir.parent / "outbox").write_text("blocking file")

        with patch(f"{_MODULE}._INBOX_DIR", Path("/dev/null/nonexistent-inbox-dir")):
            # Should not raise.
            m._queue_push_confirmation(
                chat_id="115",
                scope="workspace",
                connected_text="Google Workspace connected.",
                fetch_preview=lambda: "preview",
            )


class TestDedupeGuard:
    def test_second_push_within_window_is_a_no_op(self, isolated_dirs):
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="116", scope="calendar", connected_text="connected",
            fetch_preview=lambda: "preview 1",
        )
        m._queue_push_confirmation(
            chat_id="116", scope="calendar", connected_text="connected",
            fetch_preview=lambda: "preview 2",
        )

        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 1, "A duplicate push must not queue a second confirmation"

    def test_different_scope_same_chat_id_is_not_deduped(self, isolated_dirs):
        """De-dupe key is (scope, chat_id) -- a user connecting both Calendar
        and Gmail in quick succession must get both confirmations."""
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="117", scope="calendar", connected_text="Calendar connected",
            fetch_preview=lambda: "preview",
        )
        m._queue_push_confirmation(
            chat_id="117", scope="gmail", connected_text="Gmail connected",
            fetch_preview=lambda: "preview",
        )

        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 2

    def test_different_chat_id_same_scope_is_not_deduped(self, isolated_dirs):
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="118", scope="calendar", connected_text="Calendar connected",
            fetch_preview=lambda: "preview",
        )
        m._queue_push_confirmation(
            chat_id="119", scope="calendar", connected_text="Calendar connected",
            fetch_preview=lambda: "preview",
        )

        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 2

    def test_dedupe_expires_after_ttl(self, isolated_dirs):
        """After the TTL window elapses, a repeat push is treated as new."""
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        with patch(f"{_MODULE}._CONFIRM_DEDUPE_TTL_SECONDS", 0.05):
            m._queue_push_confirmation(
                chat_id="120", scope="calendar", connected_text="connected",
                fetch_preview=lambda: "preview 1",
            )
            time_module.sleep(0.1)
            m._queue_push_confirmation(
                chat_id="120", scope="calendar", connected_text="connected",
                fetch_preview=lambda: "preview 2",
            )

        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 2, "After TTL expiry, a repeat push must queue a new confirmation"

    def test_confirmation_already_sent_is_read_only(self, isolated_dirs):
        """_confirmation_already_sent must NOT claim the slot itself -- only
        _mark_confirmation_sent (called after a successful delivery) does.
        Calling the read-only check repeatedly must not change its answer."""
        _ = isolated_dirs
        m = _import_target()

        assert m._confirmation_already_sent("chat-x", "calendar") is False
        assert m._confirmation_already_sent("chat-x", "calendar") is False
        assert m._confirmation_already_sent("chat-x", "calendar") is False

    def test_mark_confirmation_sent_claims_the_slot(self, isolated_dirs):
        _ = isolated_dirs
        m = _import_target()

        assert m._confirmation_already_sent("chat-y", "calendar") is False
        m._mark_confirmation_sent("chat-y", "calendar")
        assert m._confirmation_already_sent("chat-y", "calendar") is True

    def test_failed_delivery_does_not_claim_dedupe_slot_so_retry_can_succeed(self, isolated_dirs):
        """Regression test for a bug an independent (Fable) review pass
        caught before merge: the original implementation claimed the de-dupe
        slot BEFORE knowing whether the outbox write succeeded, so a
        legitimate webhook retry after a transient failure would be
        silently swallowed for the rest of the TTL window -- exactly the
        "total silence" outcome this whole file exists to prevent.

        Here: first call fails (outbox path is blocked), second call
        (simulating a retry) must NOT be treated as a duplicate, and must
        succeed once the path is unblocked.
        """
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        # Break the outbox for the first attempt.
        (outbox_dir).rmdir()
        outbox_dir.write_text("blocking file, not a directory")

        m._queue_push_confirmation(
            chat_id="121", scope="calendar", connected_text="connected",
            fetch_preview=lambda: "preview",
        )
        # First attempt must NOT have claimed the de-dupe slot.
        assert m._confirmation_already_sent("121", "calendar") is False

        # Unblock the outbox and retry -- this must succeed, not be deduped.
        outbox_dir.unlink()
        outbox_dir.mkdir(parents=True)

        m._queue_push_confirmation(
            chat_id="121", scope="calendar", connected_text="connected",
            fetch_preview=lambda: "preview",
        )

        files = list(outbox_dir.glob("*.json"))
        assert len(files) == 1, "Retry after a failed delivery must succeed, not be deduped away"


class TestChannelParity:
    """Issue #2133: the confirmation must be routed to the channel that
    actually initiated the connect flow, not hardcoded to Telegram."""

    def test_default_source_is_telegram_for_backward_compatibility(self, isolated_dirs):
        """Existing call sites (and in-flight consent links generated before
        this fix) that don't pass `source` must keep behaving exactly as
        before."""
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="200", scope="calendar", connected_text="connected",
            fetch_preview=lambda: "preview",
        )

        reply = json.loads(list(outbox_dir.glob("*.json"))[0].read_text())
        assert reply["source"] == m._DEFAULT_CONFIRMATION_SOURCE
        assert reply["source"] == "telegram"

    def test_slack_source_is_used_when_provided(self, isolated_dirs):
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="C0SLACKCHAN", scope="calendar", connected_text="connected",
            fetch_preview=lambda: "preview",
            source="slack",
        )

        reply = json.loads(list(outbox_dir.glob("*.json"))[0].read_text())
        assert reply["source"] == "slack"

    def test_thread_ts_is_included_when_provided(self, isolated_dirs):
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="C0SLACKCHAN", scope="gmail", connected_text="connected",
            fetch_preview=lambda: "preview",
            source="slack",
            thread_ts="1700000000.000100",
        )

        reply = json.loads(list(outbox_dir.glob("*.json"))[0].read_text())
        assert reply["thread_ts"] == "1700000000.000100"

    def test_thread_ts_omitted_when_not_provided(self, isolated_dirs):
        """A Telegram (or non-threaded Slack) confirmation must not carry a
        stray null/empty thread_ts key."""
        outbox_dir, _ = isolated_dirs
        m = _import_target()

        m._queue_push_confirmation(
            chat_id="201", scope="workspace", connected_text="connected",
            fetch_preview=lambda: "preview",
        )

        reply = json.loads(list(outbox_dir.glob("*.json"))[0].read_text())
        assert "thread_ts" not in reply


class TestExtractConfirmationChannel:
    """_extract_confirmation_channel reads the (source, thread_ts) routing
    hint out of the push body's session_token, defaulting to telegram/None
    when absent (issue #2133)."""

    def test_no_session_token_defaults_to_telegram(self):
        m = _import_target()
        source, thread_ts = m._extract_confirmation_channel({})
        assert source == "telegram"
        assert thread_ts is None

    def test_session_token_without_source_defaults_to_telegram(self):
        """Backward compat with tokens issued before myownlobster.ai's
        broker starts echoing source/thread_ts back."""
        m = _import_target()
        body = {"session_token": {"instance_id": "x", "chat_id": "1", "scope": "calendar", "nonce": "n", "sig": "s", "exp": 1}}
        source, thread_ts = m._extract_confirmation_channel(body)
        assert source == "telegram"
        assert thread_ts is None

    def test_session_token_with_slack_source_and_thread_ts(self):
        m = _import_target()
        body = {
            "session_token": {
                "instance_id": "x", "chat_id": "1", "scope": "calendar",
                "nonce": "n", "sig": "s", "exp": 1,
                "source": "slack", "thread_ts": "1700000000.000100",
            }
        }
        source, thread_ts = m._extract_confirmation_channel(body)
        assert source == "slack"
        assert thread_ts == "1700000000.000100"

    def test_malformed_source_falls_back_to_telegram(self):
        """A non-string source (bad broker data, or an attempted injection)
        must never propagate -- fall back to the safe default."""
        m = _import_target()
        body = {"session_token": {"source": 12345}}
        source, _ = m._extract_confirmation_channel(body)
        assert source == "telegram"
