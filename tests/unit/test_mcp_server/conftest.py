"""
Shared fixtures for test_mcp_server unit tests.

Transport-level HTTP mock
-------------------------
`handle_write_observation` calls `_emit_debug_observation` → `_send_telegram_direct`
which fires a real urllib HTTP request when LOBSTER_DEBUG=true is set on the host.
The `block_outbound_http` fixture patches urllib.request.urlopen at the transport
level so no real network call can escape this test suite, regardless of how debug
mode flags are set in any individual test.

The fixture is auto-used (session-scoped) so every test in this package is covered
without requiring per-test opt-in.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True, scope="session")
def block_outbound_http():
    """Patch urllib.request.urlopen to prevent any real HTTP calls from tests.

    Targets the exact import used by _send_telegram_direct inside inbox_server.py:
      import urllib.request as _urllib_request
      ...
      with _urllib_request.urlopen(req, timeout=5):

    Patching `urllib.request.urlopen` intercepts that call at the transport layer
    regardless of which debug-mode flags individual tests set.
    """
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_response):
        yield
