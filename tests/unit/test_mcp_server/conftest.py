"""
Shared fixtures for test_mcp_server unit tests.

Debug alert isolation
---------------------
The `isolate_debug_config` fixture redirects _CONFIG_DIR to an empty temp dir
so that debug-alert resolution reads no config file. This prevents live host
credentials (TELEGRAM_BOT_TOKEN, LOBSTER_SLACK_BOT_TOKEN) from being used
during tests, ensuring debug alerts are disabled even when LOBSTER_DEBUG=true
is set in the environment.

It also clears LOBSTER_DEBUG from the test environment so that debug-mode
features (e.g. source label injection in send_reply, #1789) do not affect
tests that are not specifically testing debug behaviour.  Tests that need
LOBSTER_DEBUG=true must set it explicitly via ``patch.dict(os.environ,
{"LOBSTER_DEBUG": "true"})``.

Note: the older _DEBUG_RESOLVED / _DEBUG_MODE / _DEBUG_ALERTS_ENABLED globals
were removed as part of the debug observability refactor (issue #891). Only
_CONFIG_DIR isolation is needed now.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_debug_config(tmp_path):
    """Redirect _CONFIG_DIR to an empty temp dir and clear LOBSTER_DEBUG.

    Pointing _CONFIG_DIR at an empty directory means no TELEGRAM_BOT_TOKEN or
    LOBSTER_SLACK_BOT_TOKEN is found during config resolution, so debug alerts
    stay disabled even when LOBSTER_DEBUG=true is set in the host environment.

    Clearing LOBSTER_DEBUG prevents debug-mode features (such as source label
    injection in send_reply) from affecting tests that are not explicitly
    testing debug behaviour.
    """
    with patch("src.mcp.inbox_server._CONFIG_DIR", tmp_path):
        # Remove LOBSTER_DEBUG from the environment for the duration of each
        # test.  Tests that need it must patch it back in themselves.
        env_without_debug = {k: v for k, v in os.environ.items() if k != "LOBSTER_DEBUG"}
        with patch.dict(os.environ, env_without_debug, clear=True):
            yield
