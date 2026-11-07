"""
Regression tests for issue #2323: wait_for_messages must not be exempt from
the HTTP-mode session guard once a dispatcher session is already tagged.

Before this fix, `_dispatch_tool` unconditionally exempted `wait_for_messages`
from `_is_main_http_session()` ("wait_for_messages is always exempted (Option A
tagging)"), and `handle_wait_for_messages` unconditionally re-tagged whatever
session called it as the dispatcher and force-replaced the dispatcher lock
even when another session's lock was actively held. Together these let a
finished background subagent call `wait_for_messages()` after already calling
`write_result`, silently steal the dispatcher session tag and the dispatcher
lock, and run as a second rogue dispatcher loop against the shared inbox for
~75 minutes (the exact incident described in issue #2323).

These tests verify:
1. `_dispatch_tool` blocks a second session's `wait_for_messages` call once a
   dispatcher is already tagged (the core regression).
2. `_dispatch_tool` still allows the bootstrap case: the first call from any
   session when no dispatcher is tagged yet (Option A/C, unchanged).
3. `handle_wait_for_messages` does not re-tag the dispatcher session when one
   is already tagged (no more unconditional Option A re-tag).
4. `handle_wait_for_messages` does not blindly take over an actively-held
   dispatcher lock belonging to a different live session.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).parents[3]
for _p in [
    str(_ROOT / "src" / "mcp"),
    str(_ROOT / "src" / "agents"),
    str(_ROOT / "src"),
    str(_ROOT / "src" / "utils"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import src.mcp.inbox_server as inbox_server  # noqa: E402


async def _async_result(value):
    """Return value from an awaitable — lets a MagicMock stand in for an
    async handler function when used as `return_value=_async_result(...)`."""
    return value


# ---------------------------------------------------------------------------
# _dispatch_tool: wait_for_messages is guarded like every other guarded tool
# ---------------------------------------------------------------------------


class TestDispatchToolBlocksSecondSessionWFM:
    def test_second_session_wait_for_messages_is_blocked(self):
        """Once a dispatcher is tagged, a different session calling
        wait_for_messages must be blocked, not silently allowed through."""
        mock_session_manager = MagicMock()
        mock_session_manager.has_session.return_value = True

        with patch.multiple(
            inbox_server,
            _http_session_manager=mock_session_manager,
            _dispatcher_session_id="dispatcher-session-001",
        ), patch.object(
            inbox_server, "_get_current_http_session_id",
            return_value="rogue-subagent-session-002",
        ), patch.object(
            inbox_server, "handle_wait_for_messages",
            new=MagicMock(side_effect=AssertionError(
                "handle_wait_for_messages must not be reached — the guard "
                "should have blocked this call before dispatch"
            )),
        ):
            result = asyncio.run(
                inbox_server._dispatch_tool("wait_for_messages", {})
            )

        # Blocked: session guard error text, not the real handler's output.
        assert "SESSION GUARD" in result[0].text
        assert "wait_for_messages" in result[0].text

    def test_dispatcher_session_itself_still_allowed(self):
        """The tagged dispatcher session calling wait_for_messages again
        must still pass the guard (no regression for the normal loop)."""
        mock_session_manager = MagicMock()
        mock_handler = MagicMock(return_value=_async_result(["ok"]))

        with patch.multiple(
            inbox_server,
            _http_session_manager=mock_session_manager,
            _dispatcher_session_id="dispatcher-session-001",
        ), patch.object(
            inbox_server, "_get_current_http_session_id",
            return_value="dispatcher-session-001",
        ), patch.object(
            inbox_server, "handle_wait_for_messages", new=mock_handler,
        ):
            result = asyncio.run(
                inbox_server._dispatch_tool("wait_for_messages", {})
            )

        mock_handler.assert_called_once()
        assert result == ["ok"]

    def test_bootstrap_case_unchanged_no_dispatcher_tagged_yet(self):
        """Option A/C bootstrap: when no dispatcher is tagged yet, the first
        caller of wait_for_messages is still allowed through and gets
        auto-tagged — this must be unchanged by the fix."""
        mock_session_manager = MagicMock()
        mock_handler = MagicMock(return_value=_async_result(["ok"]))

        def _fake_tag(session_id):
            # Mirror the real _tag_dispatcher_session side effect so the
            # subsequent _is_main_http_session() check in the same call sees
            # the update, exactly as it would against the real module global.
            inbox_server._dispatcher_session_id = session_id

        with patch.multiple(
            inbox_server,
            _http_session_manager=mock_session_manager,
            _dispatcher_session_id=None,
        ), patch.object(
            inbox_server, "_get_current_http_session_id",
            return_value="new-dispatcher-session",
        ), patch.object(
            inbox_server, "_tag_dispatcher_session", side_effect=_fake_tag,
        ) as mock_tag, patch.object(
            inbox_server, "handle_wait_for_messages", new=mock_handler,
        ):
            result = asyncio.run(
                inbox_server._dispatch_tool("wait_for_messages", {})
            )

        mock_tag.assert_called_once_with("new-dispatcher-session")
        mock_handler.assert_called_once()
        assert result == ["ok"]


# ---------------------------------------------------------------------------
# handle_wait_for_messages: no unconditional re-tag / lock takeover
# ---------------------------------------------------------------------------


class TestHandleWaitForMessagesDoesNotStealDispatcherRole:
    @pytest.fixture
    def inbox_dir(self, tmp_path):
        d = tmp_path / "inbox"
        d.mkdir()
        return d

    def test_does_not_retag_when_dispatcher_already_set(self, inbox_dir):
        """A session other than the tagged dispatcher calling
        handle_wait_for_messages directly must not overwrite the dispatcher
        tag (defense in depth — _dispatch_tool's guard should already have
        rejected this call, but the handler itself must not assume that)."""
        mock_session_manager = MagicMock()
        mock_claims_db = MagicMock()
        mock_claims_db.get_dispatcher_lock.return_value = {
            "session_id": "dispatcher-session-001",
            "locked_at": "2026-11-07T00:00:00Z",
        }
        mock_session_manager.has_session.return_value = True

        with patch.multiple(
            inbox_server,
            INBOX_DIR=inbox_dir,
            _http_session_manager=mock_session_manager,
            _dispatcher_session_id="dispatcher-session-001",
            _claims_db=mock_claims_db,
        ), patch.object(
            inbox_server, "_get_current_http_session_id",
            return_value="rogue-subagent-session-002",
        ), patch.object(
            inbox_server, "_tag_dispatcher_session",
        ) as mock_tag:
            asyncio.run(inbox_server.handle_wait_for_messages({"timeout": 1}))

        # The dangerous re-tag call must never happen once a dispatcher is set.
        mock_tag.assert_not_called()

    def test_does_not_force_replace_lock_when_actively_held_by_another(self, inbox_dir):
        """force_replace_dispatcher_lock must not be called when the lock is
        actively held by a different, live session (issue #2323 — this is
        what let the rogue subagent dispossess the real dispatcher)."""
        mock_session_manager = MagicMock()
        mock_session_manager.has_session.return_value = True  # old holder is alive
        mock_claims_db = MagicMock()
        mock_claims_db.get_dispatcher_lock.return_value = {
            "session_id": "dispatcher-session-001",
            "locked_at": "2026-11-07T00:00:00Z",
        }

        with patch.multiple(
            inbox_server,
            INBOX_DIR=inbox_dir,
            _http_session_manager=mock_session_manager,
            _dispatcher_session_id="dispatcher-session-001",
            _claims_db=mock_claims_db,
        ), patch.object(
            inbox_server, "_get_current_http_session_id",
            return_value="rogue-subagent-session-002",
        ):
            asyncio.run(inbox_server.handle_wait_for_messages({"timeout": 1}))

        mock_claims_db.force_replace_dispatcher_lock.assert_not_called()

    def test_still_takes_over_stale_lock(self, inbox_dir):
        """A lock held by a session that is no longer active (dead/reconnected
        dispatcher) must still be taken over — this legitimate recovery path
        must be unchanged by the fix."""
        mock_session_manager = MagicMock()
        mock_session_manager.has_session.return_value = False  # old holder is dead
        mock_claims_db = MagicMock()
        mock_claims_db.get_dispatcher_lock.return_value = {
            "session_id": "old-dead-session",
            "locked_at": "2026-11-07T00:00:00Z",
        }

        with patch.multiple(
            inbox_server,
            INBOX_DIR=inbox_dir,
            _http_session_manager=mock_session_manager,
            _dispatcher_session_id="old-dead-session",
            _claims_db=mock_claims_db,
        ), patch.object(
            inbox_server, "_get_current_http_session_id",
            return_value="new-dispatcher-session",
        ):
            asyncio.run(inbox_server.handle_wait_for_messages({"timeout": 1}))

        mock_claims_db.force_replace_dispatcher_lock.assert_called_once_with(
            "new-dispatcher-session"
        )
