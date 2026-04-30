"""
Tests for HTTP session idle timeout configuration (issue #1823, fix #1876).

The MCP server's stateful HTTP transport must be constructed with
session_idle_timeout=SESSION_IDLE_TIMEOUT_SECONDS to prevent anyio task
group accumulation in the StreamableHTTPSessionManager — the root cause of
the 4-minute asyncio stall observed on 2026-04-26.

The constant SESSION_IDLE_TIMEOUT_SECONDS must be defined at module level so
health checks and tests can reference it without hardcoding the literal.

As of issue #1876, the value is 1200s (20 min) to reap zombie subagent sessions
quickly. The dispatcher session is NOT reaped at this timeout because
handle_wait_for_messages() explicitly extends its idle_scope.deadline to 72000s
on every WFM entry — making the dispatcher exempt from the global reap policy.
"""

import asyncio
import contextlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from src.mcp.inbox_server import SESSION_IDLE_TIMEOUT_SECONDS


class TestSessionIdleTimeoutConstant:
    """SESSION_IDLE_TIMEOUT_SECONDS must be exported from inbox_server."""

    def test_constant_is_defined_and_correct(self):
        """MODULE must export SESSION_IDLE_TIMEOUT_SECONDS == 1200 (20 min zombie reap window).

        The dispatcher is protected separately: handle_wait_for_messages() extends
        its idle_scope.deadline to 72000s on every WFM entry (issue #1876).
        """
        assert SESSION_IDLE_TIMEOUT_SECONDS == 1200, (
            f"Expected SESSION_IDLE_TIMEOUT_SECONDS == 1200 "
            f"(20-min zombie-reap window; dispatcher deadline is extended separately "
            f"to 72000s in handle_wait_for_messages — issue #1876); "
            f"got {SESSION_IDLE_TIMEOUT_SECONDS}"
        )


class TestHttpSessionManagerIdleTimeout:
    """The stateful HTTP session manager must be constructed with idle timeout."""

    def test_main_passes_session_idle_timeout_to_constructor(self, monkeypatch):
        """main() in HTTP mode must pass session_idle_timeout to StreamableHTTPSessionManager.

        Without session_idle_timeout, anyio task groups accumulate for each
        long-lived dispatcher session and can stall the event loop for minutes
        (issue #1823).  The value is 1200s (20 min) to reap zombie subagent sessions;
        the dispatcher's own deadline is extended to 72000s on each WFM entry (issue #1876).
        """
        captured_calls = []

        class CapturingSessionManager:
            """Records constructor kwargs so we can assert on them."""
            def __init__(self, **kwargs):
                captured_calls.append(kwargs)

            @contextlib.asynccontextmanager
            async def run(self):
                yield

        # inbox_server imports StreamableHTTPSessionManager locally inside main()
        # via: from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        # Patching the source module is the correct intercept point.
        mock_streamable_module = MagicMock()
        mock_streamable_module.StreamableHTTPSessionManager = CapturingSessionManager

        mock_uvicorn_server = MagicMock()
        mock_uvicorn_server.serve = AsyncMock(return_value=None)

        monkeypatch.setitem(sys.modules, "mcp.server.streamable_http_manager", mock_streamable_module)
        monkeypatch.setattr(sys, "argv", ["inbox_server.py", "--http"])

        with (
            patch("uvicorn.Config", return_value=MagicMock()),
            patch("uvicorn.Server", return_value=mock_uvicorn_server),
        ):
            import src.mcp.inbox_server as mod
            asyncio.run(mod.main())

        assert captured_calls, (
            "StreamableHTTPSessionManager was never constructed — "
            "did main() fail to enter HTTP mode?"
        )
        kwargs = captured_calls[0]

        assert "session_idle_timeout" in kwargs, (
            "StreamableHTTPSessionManager must receive session_idle_timeout; "
            "omitting it causes anyio task group accumulation (issue #1823)"
        )
        assert kwargs["session_idle_timeout"] == SESSION_IDLE_TIMEOUT_SECONDS, (
            f"session_idle_timeout must be {SESSION_IDLE_TIMEOUT_SECONDS}s "
            f"(from inbox_server.SESSION_IDLE_TIMEOUT_SECONDS); "
            f"got {kwargs['session_idle_timeout']}"
        )
        assert kwargs.get("stateless") is False, (
            "The dispatcher session manager must be stateful (stateless=False); "
            "combining stateless=True with session_idle_timeout raises RuntimeError"
        )
