"""
Tests for alwaysLoad metadata on core MCP tools.

Context
-------
Claude Code defers all MCP tools by default (isMcp === true in the CC binary).
When a deferred tool's schema has not been loaded via ToolSearch, the CC client
coerces all arguments to strings, causing InputValidationError at runtime:

    InputValidationError: '10' is not of type 'integer'
    InputValidationError: 'true' is not of type 'boolean'

The CC binary reads `_meta["anthropic/alwaysLoad"] === true` when mapping
tool-list responses. If this flag is set, `isDeferredTool()` returns false and
the tool is sent to the API with its full schema on every turn — no ToolSearch
required.

The 8 core tools below are called unconditionally on every dispatcher restart
before any ToolSearch invocation. They must carry alwaysLoad=True to prevent
startup failures. (PR #1703 added this; this test guards against regression.)

ALWAYS_LOAD_TOOLS
-----------------
The constant below is the source of truth for which tools must be marked.
Both the test assertions and any future code changes should reference it.
"""

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure src/mcp is on sys.path for inbox_server sibling imports
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import src.mcp.inbox_server  # noqa: F401  — pre-load for patch resolution

# The 8 core tools that must opt out of deferred loading.
# These are called unconditionally during dispatcher startup, before any
# ToolSearch invocation can pre-load their schemas.
ALWAYS_LOAD_TOOLS = frozenset(
    {
        "wait_for_messages",
        "check_inbox",
        "send_reply",
        "mark_processed",
        "mark_processing",
        "session_start",
        "get_conversation_history",
        "list_rules",
    }
)


class TestAlwaysLoadMetadata:
    """Verify that core tools carry the alwaysLoad opt-out flag."""

    @pytest.fixture
    def all_tools(self):
        """Return the full tool list from inbox_server.list_tools()."""
        from src.mcp.inbox_server import list_tools

        return asyncio.run(list_tools())

    def test_always_load_tools_exist_in_tool_list(self, all_tools):
        """All 8 ALWAYS_LOAD_TOOLS are present in the tool registry."""
        tool_names = {t.name for t in all_tools}
        missing = ALWAYS_LOAD_TOOLS - tool_names
        assert not missing, (
            f"Tools missing from list_tools() output: {missing}. "
            "Each name in ALWAYS_LOAD_TOOLS must correspond to a registered tool."
        )

    def test_always_load_tools_have_meta_flag_set(self, all_tools):
        """Each tool in ALWAYS_LOAD_TOOLS has _meta['anthropic/alwaysLoad'] == True.

        The CC binary checks `O._meta?.['anthropic/alwaysLoad'] === true` when
        mapping tools/list responses. If this is False or absent, the tool is
        deferred and startup tool calls fail with InputValidationError.
        """
        tool_index = {t.name: t for t in all_tools}
        failures = []

        for name in sorted(ALWAYS_LOAD_TOOLS):
            tool = tool_index[name]
            meta = tool.meta  # MCP SDK exposes _meta as .meta
            if meta is None or meta.get("anthropic/alwaysLoad") is not True:
                failures.append(
                    f"{name}: meta={meta!r} "
                    f"(expected {{'anthropic/alwaysLoad': True}})"
                )

        assert not failures, (
            "The following tools are missing alwaysLoad=True. "
            "Without it the CC client defers them and coerces args to strings, "
            "causing InputValidationError on every dispatcher restart:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )

    def test_non_core_tools_do_not_have_always_load(self, all_tools):
        """Tools not in ALWAYS_LOAD_TOOLS must NOT carry alwaysLoad=True.

        This is a soft sanity check: alwaysLoad increases token costs because
        full schemas are sent on every API call. Only tools called before any
        ToolSearch invocation should carry the flag.
        """
        unexpected = []
        for tool in all_tools:
            if tool.name in ALWAYS_LOAD_TOOLS:
                continue
            meta = tool.meta
            if meta is not None and meta.get("anthropic/alwaysLoad") is True:
                unexpected.append(tool.name)

        assert not unexpected, (
            "The following tools have alwaysLoad=True but are NOT in ALWAYS_LOAD_TOOLS. "
            "This increases token cost for every API call. If these tools genuinely "
            "need to be pre-loaded, add them to ALWAYS_LOAD_TOOLS:\n"
            + "\n".join(f"  - {t}" for t in sorted(unexpected))
        )
