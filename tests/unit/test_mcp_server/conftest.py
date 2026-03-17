"""
Shared fixtures for test_mcp_server unit tests.

Debug alert isolation
---------------------
`_emit_debug_observation` writes to INBOX_DIR when LOBSTER_DEBUG=true is set
in the host environment AND a valid admin channel is configured. On a live
lobster host both conditions are met, which would cause unrelated tests to see
extra inbox files.

The `isolate_debug_config` fixture redirects _CONFIG_DIR to a non-existent
path so _resolve_debug_config() finds no config file and leaves
_DEBUG_ALERTS_ENABLED=False (no valid destination resolved). Individual tests
in test_debug_alerts.py that need debug mode active patch the relevant globals
explicitly via patch.multiple, bypassing config resolution entirely.
"""

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_debug_config(tmp_path):
    """Redirect _CONFIG_DIR to an empty temp dir for each test.

    _resolve_debug_config reads config.env from _CONFIG_DIR. Pointing it at an
    empty directory means no TELEGRAM_BOT_TOKEN or LOBSTER_SLACK_BOT_TOKEN is
    found, so _DEBUG_ALERTS_ENABLED stays False even when LOBSTER_DEBUG=true is
    set in the host environment.

    Also resets the lazy-resolution globals so each test starts clean.

    Tests in test_debug_alerts.py that need debug alerts active short-circuit
    _resolve_debug_config by patching _DEBUG_RESOLVED=True and _DEBUG_MODE=True
    directly via patch.multiple — those patches take effect before this fixture's
    side effects are relevant.
    """
    with patch.multiple(
        "src.mcp.inbox_server",
        _CONFIG_DIR=tmp_path,
        _DEBUG_RESOLVED=False,
        _DEBUG_MODE=None,
        _DEBUG_ALERTS_ENABLED=False,
        _DEBUG_OWNER_CHAT_ID=None,
        _DEBUG_OWNER_SOURCE="telegram",
    ):
        yield
