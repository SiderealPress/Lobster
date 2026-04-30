"""
Unit tests for graceful session-reap notification (issue #1876).

When session_idle_timeout fires in GracefulSessionManager._handle_stateful_request(),
the server must write a `session-reap` message to the inbox BEFORE terminating the
idle session.  This lets the dispatcher call wait_for_messages() cleanly rather than
receiving an unexplained "Session not found" 404.

Root cause: session_idle_timeout applies equally to ALL sessions — the dispatcher
and a crashed subagent look identical to the SDK.  The graceful notification lets
the dispatcher handle its own reap cleanly instead of crashing.

Design:
- GracefulSessionManager subclasses StreamableHTTPSessionManager
- Overrides _handle_stateful_request so the run_server coroutine calls
  _write_session_reap_notification() when idle_scope.cancelled_caught is True
- Message type: "session-reap" (not "compact-reminder" — dispatcher needs no
  re-orientation, just a signal to call wait_for_messages() again)
- Non-fatal: if the inbox write fails, proceed with reap anyway

Named constants matching the spec:
- SESSION_REAP_MESSAGE_TYPE = "session-reap"
- SESSION_REAP_MESSAGE_PREFIX = "session-reap-"
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, call
from datetime import datetime, timezone

import pytest

_SRC_MCP_DIR = Path(__file__).parents[2] / "src" / "mcp"
_SERVER_PATH = _SRC_MCP_DIR / "inbox_server.py"

# Named constants matching the spec
SESSION_REAP_MESSAGE_TYPE = "session-reap"
SESSION_REAP_MESSAGE_PREFIX = "session-reap-"


# ---------------------------------------------------------------------------
# Helper: load the GracefulSessionManager class and _write_session_reap_notification
# function from inbox_server module in a test-safe way.
# ---------------------------------------------------------------------------

def _get_inbox_server_module():
    """Import inbox_server module, adding src/mcp to path if needed."""
    mod = sys.modules.get("inbox_server")
    if mod is None:
        if str(_SRC_MCP_DIR) not in sys.path:
            sys.path.insert(0, str(_SRC_MCP_DIR))
        import importlib
        mod = importlib.import_module("inbox_server")
    return mod


# ---------------------------------------------------------------------------
# Tests for _write_session_reap_notification()
# ---------------------------------------------------------------------------

class TestWriteSessionReapNotification:
    """_write_session_reap_notification() writes a session-reap message to the inbox."""

    def test_writes_session_reap_message_to_inbox(self, tmp_path):
        """When called, a session-reap message is written to the inbox dir."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()
        with patch.object(mod, "INBOX_DIR", inbox_dir):
            mod._write_session_reap_notification()

        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        assert len(files) == 1, f"Expected exactly one {SESSION_REAP_MESSAGE_PREFIX}*.json file, got {files}"

    def test_message_has_correct_type(self, tmp_path):
        """Written message must have type 'session-reap', NOT 'compact-reminder'."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()
        with patch.object(mod, "INBOX_DIR", inbox_dir):
            mod._write_session_reap_notification()

        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        msg = json.loads(files[0].read_text())
        assert msg["type"] == SESSION_REAP_MESSAGE_TYPE, (
            f"Expected type={SESSION_REAP_MESSAGE_TYPE!r}, got {msg['type']!r}. "
            "This must NOT be 'compact-reminder' — the dispatcher was not restarted, "
            "it was idle-reaped. No bootup re-read is required."
        )

    def test_message_has_system_source_and_zero_chat_id(self, tmp_path):
        """Message must be an internal system message (source='system', chat_id=0)."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()
        with patch.object(mod, "INBOX_DIR", inbox_dir):
            mod._write_session_reap_notification()

        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        msg = json.loads(files[0].read_text())
        assert msg["source"] == "system"
        assert msg["chat_id"] == 0
        assert msg.get("task_origin") == "internal"

    def test_message_text_indicates_wfm_reconnect(self, tmp_path):
        """Message text must cue the dispatcher to call wait_for_messages(), not re-read bootup."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()
        with patch.object(mod, "INBOX_DIR", inbox_dir):
            mod._write_session_reap_notification()

        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        msg = json.loads(files[0].read_text())
        text = msg["text"].lower()
        # Should mention reconnect/WFM/closing — NOT "SESSION LOST" or "re-orient"
        assert any(word in text for word in ("reconnect", "closing", "wfm", "connection")), (
            f"Message text should cue reconnect, not re-orientation. Got: {msg['text']!r}"
        )
        # Must NOT say SESSION LOST — that's for actual restarts
        assert "session lost" not in text, (
            f"Message text must not say 'SESSION LOST' — this is an idle reap, not a crash. "
            f"Got: {msg['text']!r}"
        )

    def test_message_has_valid_timestamp_and_id(self, tmp_path):
        """Written message must have a valid ISO timestamp and prefixed id."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()
        with patch.object(mod, "INBOX_DIR", inbox_dir):
            mod._write_session_reap_notification()

        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        msg = json.loads(files[0].read_text())
        assert msg["id"].startswith(SESSION_REAP_MESSAGE_PREFIX)
        # Must parse as ISO datetime without raising
        dt = datetime.fromisoformat(msg["timestamp"])
        assert dt.tzinfo is not None, "Timestamp must be timezone-aware"

    def test_does_not_raise_when_inbox_write_fails(self, tmp_path):
        """If inbox write fails, the function must NOT raise — the reap must still proceed."""
        inbox_dir = tmp_path / "inbox"
        # Deliberately omit mkdir() so atomic_write_json will fail

        mod = _get_inbox_server_module()
        with patch.object(mod, "INBOX_DIR", inbox_dir):
            # Must not raise even if the inbox dir is missing
            mod._write_session_reap_notification()  # should not raise

    def test_suppressed_in_dev_mode(self, tmp_path):
        """LOBSTER_DEV_MODE=true must suppress the session-reap notification."""
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()
        old_dev = os.environ.get("LOBSTER_DEV_MODE")
        try:
            os.environ["LOBSTER_DEV_MODE"] = "true"
            with patch.object(mod, "INBOX_DIR", inbox_dir):
                mod._write_session_reap_notification()
        finally:
            if old_dev is None:
                os.environ.pop("LOBSTER_DEV_MODE", None)
            else:
                os.environ["LOBSTER_DEV_MODE"] = old_dev

        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        assert len(files) == 0, "Dev mode must suppress session-reap notification"


# ---------------------------------------------------------------------------
# Tests for GracefulSessionManager
# ---------------------------------------------------------------------------

class TestGracefulSessionManagerExists:
    """GracefulSessionManager subclass is defined in inbox_server."""

    def test_graceful_session_manager_class_exists(self):
        """inbox_server must define a GracefulSessionManager class."""
        mod = _get_inbox_server_module()
        assert hasattr(mod, "GracefulSessionManager"), (
            "GracefulSessionManager class not found in inbox_server.py"
        )

    def test_graceful_session_manager_is_subclass(self):
        """GracefulSessionManager must be a subclass of StreamableHTTPSessionManager."""
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        mod = _get_inbox_server_module()
        GracefulSessionManager = mod.GracefulSessionManager
        assert issubclass(GracefulSessionManager, StreamableHTTPSessionManager), (
            "GracefulSessionManager must subclass StreamableHTTPSessionManager"
        )

    def test_main_uses_graceful_session_manager(self):
        """inbox_server.py must instantiate GracefulSessionManager, not the base class."""
        src = _SERVER_PATH.read_text()
        assert "GracefulSessionManager(" in src, (
            "inbox_server.py must use GracefulSessionManager() not StreamableHTTPSessionManager() "
            "directly so the graceful reap hook fires."
        )
        # Also verify the base class is still imported (for the subclass to inherit)
        assert "StreamableHTTPSessionManager" in src


# ---------------------------------------------------------------------------
# Integration-style test: GracefulSessionManager calls _write_session_reap_notification
# when idle_scope.cancelled_caught is True
# ---------------------------------------------------------------------------

class TestGracefulSessionManagerReapCallback:
    """GracefulSessionManager writes session-reap notification on idle timeout."""

    @pytest.mark.asyncio
    async def test_session_reap_notification_called_on_idle_timeout(self, tmp_path):
        """When idle_scope.cancelled_caught=True, _write_session_reap_notification fires.

        This test exercises the async path without actually running a full HTTP server.
        We mock the MCP app and the transport to simulate what happens when the idle
        cancel scope catches a cancellation.
        """
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()

        # Track calls to _write_session_reap_notification
        reap_calls = []
        original = mod._write_session_reap_notification

        def _tracking_write():
            reap_calls.append(True)
            # Call original with patched INBOX_DIR
            original()

        import anyio
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        GracefulSessionManager = mod.GracefulSessionManager

        # Create a mock MCP app whose run() immediately returns (simulating idle timeout
        # by relying on the cancel scope being pre-cancelled below).
        mock_app = MagicMock()
        mock_app.create_initialization_options.return_value = {}

        async def mock_run(read, write, opts, stateless=False):
            # Simulate idling until cancelled by the scope
            await anyio.sleep_forever()

        mock_app.run = mock_run

        manager = GracefulSessionManager(
            app=mock_app,
            stateless=False,
            session_idle_timeout=0.05,  # 50ms — fires almost immediately in test
        )

        # We only test that _write_session_reap_notification is invoked on idle reap.
        # Simulate the full session lifecycle: run the manager, connect a session,
        # wait for the idle timeout to fire, then verify the notification was written.
        with patch.object(mod, "INBOX_DIR", inbox_dir), \
             patch.object(mod, "_write_session_reap_notification", side_effect=_tracking_write):

            async with anyio.create_task_group() as tg:
                async with manager.run():
                    # Fake a new-session ASGI request so _handle_stateful_request
                    # creates a session and starts the run_server task.
                    scope = {
                        "type": "http",
                        "method": "POST",
                        "path": "/mcp",
                        "query_string": b"",
                        "headers": [],
                    }

                    # receive() returns a minimal HTTP body
                    async def receive():
                        return {"type": "http.request", "body": b'{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}', "more_body": False}

                    sent_responses = []

                    async def send(event):
                        sent_responses.append(event)

                    # Trigger session creation (POST /mcp with no session-id header)
                    try:
                        await manager.handle_request(scope, receive, send)
                    except Exception:
                        pass  # Expected — we're not running a real server

                    # Wait long enough for the 50ms idle timeout to fire and the
                    # reap notification to be written
                    await anyio.sleep(0.3)

        # After the session manager exits, verify the notification was called
        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        assert len(files) >= 1, (
            "Expected at least one session-reap notification in the inbox after idle timeout. "
            f"reap_calls={reap_calls}, files={files}"
        )

    @pytest.mark.asyncio
    async def test_no_reap_notification_when_app_exits_normally(self, tmp_path):
        """When app.run() returns without idle timeout, no session-reap is written.

        Normal termination (e.g. client disconnect) should NOT trigger the notification.
        Only idle-timeout reap fires it.
        """
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(parents=True)

        mod = _get_inbox_server_module()

        import anyio
        GracefulSessionManager = mod.GracefulSessionManager

        mock_app = MagicMock()
        mock_app.create_initialization_options.return_value = {}

        async def mock_run_normal_exit(read, write, opts, stateless=False):
            # Returns immediately — simulates normal app shutdown (no idle timeout)
            return

        mock_app.run = mock_run_normal_exit

        manager = GracefulSessionManager(
            app=mock_app,
            stateless=False,
            session_idle_timeout=60.0,  # long — won't fire during this test
        )

        with patch.object(mod, "INBOX_DIR", inbox_dir):
            async with anyio.create_task_group() as tg:
                async with manager.run():
                    scope = {
                        "type": "http",
                        "method": "POST",
                        "path": "/mcp",
                        "query_string": b"",
                        "headers": [],
                    }

                    async def receive():
                        return {"type": "http.request", "body": b'{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}', "more_body": False}

                    async def send(event):
                        pass

                    try:
                        await manager.handle_request(scope, receive, send)
                    except Exception:
                        pass

                    # Brief pause — no reap should fire
                    await anyio.sleep(0.1)

        # Normal exit must NOT write a session-reap notification
        files = list(inbox_dir.glob(f"{SESSION_REAP_MESSAGE_PREFIX}*.json"))
        assert len(files) == 0, (
            "session-reap notification must NOT be written on normal app exit. "
            f"files={files}"
        )
