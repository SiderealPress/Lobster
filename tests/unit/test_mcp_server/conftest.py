"""
Shared fixtures for test_mcp_server unit tests.

Debug alert isolation
---------------------
The `isolate_debug_config` fixture redirects _CONFIG_DIR to an empty temp dir
so that debug-alert resolution reads no config file. This prevents live host
credentials (TELEGRAM_BOT_TOKEN, LOBSTER_SLACK_BOT_TOKEN) from being used
during tests, ensuring debug alerts are disabled even when LOBSTER_DEBUG=true
is set in the environment.

Note: the older _DEBUG_RESOLVED / _DEBUG_MODE / _DEBUG_ALERTS_ENABLED globals
were removed as part of the debug observability refactor (issue #891). Only
_CONFIG_DIR isolation is needed now.
"""

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_debug_config(tmp_path):
    """Redirect _CONFIG_DIR to an empty temp dir for each test.

    Pointing _CONFIG_DIR at an empty directory means no TELEGRAM_BOT_TOKEN or
    LOBSTER_SLACK_BOT_TOKEN is found during config resolution, so debug alerts
    stay disabled even when LOBSTER_DEBUG=true is set in the host environment.
    """
    with patch("src.mcp.inbox_server._CONFIG_DIR", tmp_path):
        yield
