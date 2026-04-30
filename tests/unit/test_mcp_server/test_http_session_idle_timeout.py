"""
Tests for HTTP session idle timeout configuration (issue #1823, fix #1873).

The MCP server's stateful HTTP transport must be constructed with
session_idle_timeout=SESSION_IDLE_TIMEOUT_SECONDS to prevent anyio task
group accumulation in the StreamableHTTPSessionManager — the root cause of
the 4-minute asyncio stall observed on 2026-04-26.

The constant SESSION_IDLE_TIMEOUT_SECONDS must be defined at module level so
health checks and tests can reference it without hardcoding the literal.

The value must be >= the wait_for_messages timeout (72000s / 20h) to prevent
the MCP session from being reaped while the dispatcher is idle in WFM.
Setting this to 1800s caused dispatcher crash loops overnight (issue #1873).
"""

import asyncio
import contextlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from src.mcp.inbox_server import SESSION_IDLE_TIMEOUT_SECONDS


class TestSessionIdleTimeoutConstant:
    """SESSION_IDLE_TIMEOUT_SECONDS must be exported from inbox_server."""

    def test_constant_is_defined_and_correct(self):
        """MODULE must export SESSION_IDLE_TIMEOUT_SECONDS >= 72000 (20h, matching WFM timeout)."""
        assert SESSION_IDLE_TIMEOUT_SECONDS >= 72000, (
            f"Expected SESSION_IDLE_TIMEOUT_SECONDS >= 72000 "
            f"(20 hours, matching WFM default; 1800s caused crash loops in #1873); "
            f"got {SESSION_IDLE_TIMEOUT_SECONDS}"
        )


class TestHttpSessionManagerIdleTimeout:
    """The stateful HTTP session manager must be constructed with idle timeout."""

    def test_main_passes_session_idle_timeout_to_constructor(self, monkeypatch):
        """main() in HTTP mode must pass session_idle_timeout to StreamableHTTPSessionManager.

        Without session_idle_timeout, anyio task groups accumulate for each
        long-lived dispatcher session and can stall the event loop for minutes
        (issue #1823).  Providing idle_timeout=72000 (20h, matching WFM default)
        ensures stale sessions are reaped via anyio.CancelScope deadline while
        surviving dispatcher idle waits.  1800s was too short (#1873).
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
