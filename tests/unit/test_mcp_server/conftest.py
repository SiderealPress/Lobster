"""
Shared fixtures for test_mcp_server unit tests.

Debug alert isolation
---------------------
The _emit_debug_observation / _resolve_debug_config pattern was replaced by the
event bus (issue #891). The conftest now only patches _CONFIG_DIR to prevent
tests from reading live config files. The stale _DEBUG_RESOLVED / _DEBUG_MODE /
_DEBUG_ALERTS_ENABLED / _DEBUG_OWNER_* globals no longer exist in inbox_server.py.
"""

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_debug_config(tmp_path):
    """Redirect _CONFIG_DIR to an empty temp dir for each test.

    Prevents tests from reading the live config.env which may contain real tokens
    and trigger side effects (debug alerts, etc.).
    """
    with patch("src.mcp.inbox_server._CONFIG_DIR", tmp_path):
        yield
