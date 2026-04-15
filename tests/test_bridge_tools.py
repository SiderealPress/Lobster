"""
Tests for Layer 3 convenience tools (canonical memory readers).

Covers:
- get_priorities / get_daily_digest / get_handoff (file readers)
- get_project_context (file reader with project selection)
- list_projects (directory listing)
- Graceful fallback when files do not exist
- Path traversal rejection in get_project_context
- Pure helper functions (_read_canonical_file, _list_project_names)
- Local bridge server tool dispatch
- HTTP bridge READONLY_TOOLS allowlist inclusion
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure src/mcp is importable
# ---------------------------------------------------------------------------

SRC_MCP_DIR = Path(__file__).resolve().parent.parent / "src" / "mcp"
sys.path.insert(0, str(SRC_MCP_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_dir(tmp_path: Path) -> Path:
    """Create a temporary canonical directory with sample files."""
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "projects").mkdir()

    (canonical / "priorities.md").write_text("# Priorities\n\n1. Ship Layer 3\n")
    (canonical / "daily-digest.md").write_text("# Daily Digest\n\nAll quiet.\n")
    (canonical / "handoff.md").write_text("# Handoff\n\nLobster is running.\n")
    (canonical / "projects" / "lobster.md").write_text("# Lobster\n\nStatus: active\n")
    (canonical / "projects" / "transformers.md").write_text("# Transformers\n\nStatus: planning\n")

    return canonical


@pytest.fixture
def empty_canonical_dir(tmp_path: Path) -> Path:
    """Create an empty canonical directory (no files at all)."""
    canonical = tmp_path / "canonical-empty"
    canonical.mkdir()
    return canonical


# ===========================================================================
# Tests for pure helpers in inbox_server.py
# ===========================================================================


class TestReadCanonicalFile:
    """Tests for _read_canonical_file."""

    def test_returns_file_content_when_exists(self, canonical_dir: Path):
        from inbox_server import _read_canonical_file

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = _read_canonical_file("priorities.md", "fallback")
        assert result == "# Priorities\n\n1. Ship Layer 3\n"

    def test_returns_fallback_when_missing(self, empty_canonical_dir: Path):
        from inbox_server import _read_canonical_file

        with patch("inbox_server.CANONICAL_DIR", empty_canonical_dir):
            result = _read_canonical_file("priorities.md", "No file found.")
        assert result == "No file found."


class TestListProjectNames:
    """Tests for _list_project_names."""

    def test_returns_sorted_project_list(self, canonical_dir: Path):
        from inbox_server import _list_project_names

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = _list_project_names()
        names = [p["name"] for p in result]
        assert names == ["lobster", "transformers"]

    def test_returns_empty_list_when_no_projects_dir(self, empty_canonical_dir: Path):
        from inbox_server import _list_project_names

        with patch("inbox_server.CANONICAL_DIR", empty_canonical_dir):
            result = _list_project_names()
        assert result == []


# ===========================================================================
# Tests for VPS handler functions (inbox_server.py)
# ===========================================================================


class TestHandleGetPriorities:
    """Tests for handle_get_priorities."""

    @pytest.mark.asyncio
    async def test_returns_priorities_content(self, canonical_dir: Path):
        from inbox_server import handle_get_priorities

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_get_priorities({})
        assert len(result) == 1
        assert "Ship Layer 3" in result[0].text

    @pytest.mark.asyncio
    async def test_returns_fallback_when_missing(self, empty_canonical_dir: Path):
        from inbox_server import handle_get_priorities

        with patch("inbox_server.CANONICAL_DIR", empty_canonical_dir):
            result = await handle_get_priorities({})
        assert "No priorities file found" in result[0].text


class TestHandleGetDailyDigest:
    """Tests for handle_get_daily_digest."""

    @pytest.mark.asyncio
    async def test_returns_digest_content(self, canonical_dir: Path):
        from inbox_server import handle_get_daily_digest

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_get_daily_digest({})
        assert "All quiet" in result[0].text

    @pytest.mark.asyncio
    async def test_returns_fallback_when_missing(self, empty_canonical_dir: Path):
        from inbox_server import handle_get_daily_digest

        with patch("inbox_server.CANONICAL_DIR", empty_canonical_dir):
            result = await handle_get_daily_digest({})
        assert "No daily digest found" in result[0].text


class TestHandleGetProjectContext:
    """Tests for handle_get_project_context."""

    @pytest.mark.asyncio
    async def test_returns_project_content(self, canonical_dir: Path):
        from inbox_server import handle_get_project_context

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_get_project_context({"project": "lobster"})
        assert "Status: active" in result[0].text

    @pytest.mark.asyncio
    async def test_returns_available_projects_on_miss(self, canonical_dir: Path):
        from inbox_server import handle_get_project_context

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_get_project_context({"project": "govscan"})
        assert "No project file for 'govscan'" in result[0].text
        assert "lobster" in result[0].text
        assert "transformers" in result[0].text

    @pytest.mark.asyncio
    async def test_rejects_path_traversal_slash(self, canonical_dir: Path):
        from inbox_server import handle_get_project_context

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_get_project_context({"project": "../etc/passwd"})
        assert "invalid project name" in result[0].text

    @pytest.mark.asyncio
    async def test_rejects_path_traversal_dotdot(self, canonical_dir: Path):
        from inbox_server import handle_get_project_context

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_get_project_context({"project": "..\\secrets"})
        assert "invalid project name" in result[0].text

    @pytest.mark.asyncio
    async def test_rejects_empty_project_name(self, canonical_dir: Path):
        from inbox_server import handle_get_project_context

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_get_project_context({})
        assert "project name is required" in result[0].text

    @pytest.mark.asyncio
    async def test_returns_none_available_when_no_projects_dir(self, empty_canonical_dir: Path):
        from inbox_server import handle_get_project_context

        with patch("inbox_server.CANONICAL_DIR", empty_canonical_dir):
            result = await handle_get_project_context({"project": "lobster"})
        assert "Available: none" in result[0].text


class TestHandleListProjects:
    """Tests for handle_list_projects."""

    @pytest.mark.asyncio
    async def test_returns_project_list_json(self, canonical_dir: Path):
        from inbox_server import handle_list_projects

        with patch("inbox_server.CANONICAL_DIR", canonical_dir):
            result = await handle_list_projects({})
        parsed = json.loads(result[0].text)
        names = [p["name"] for p in parsed]
        assert "lobster" in names
        assert "transformers" in names

    @pytest.mark.asyncio
    async def test_returns_message_when_empty(self, empty_canonical_dir: Path):
        from inbox_server import handle_list_projects

        with patch("inbox_server.CANONICAL_DIR", empty_canonical_dir):
            result = await handle_list_projects({})
        assert "No project files found" in result[0].text


# ===========================================================================
# Tests for HTTP bridge allowlist
# ===========================================================================


class TestHttpBridgeAllowlist:
    """Verify convenience tools are in the READONLY_TOOLS set."""

    def test_convenience_tools_in_readonly_set(self):
        # inbox_server_http calls sys.exit(1) if MCP_HTTP_TOKEN is not set;
        # supply a dummy token so the module can be imported.
        import os
        os.environ.setdefault("MCP_HTTP_TOKEN", "test-token-for-import")
        from inbox_server_http import READONLY_TOOLS

        expected = {"get_priorities", "get_project_context", "get_daily_digest", "list_projects"}
        assert expected.issubset(READONLY_TOOLS)


# ===========================================================================
# Tests for local bridge pure helpers
# ===========================================================================


class TestLocalBridgeHelpers:
    """Tests for pure functions in lobster_bridge_local.py."""

    def test_read_canonical_file_returns_content(self, canonical_dir: Path):
        from lobster_bridge_local import _read_canonical_file

        result = _read_canonical_file(canonical_dir, "priorities.md", "fallback")
        assert result == "# Priorities\n\n1. Ship Layer 3\n"

    def test_read_canonical_file_returns_fallback(self, empty_canonical_dir: Path):
        from lobster_bridge_local import _read_canonical_file

        result = _read_canonical_file(empty_canonical_dir, "priorities.md", "fallback")
        assert result == "fallback"

    def test_list_project_names_returns_sorted(self, canonical_dir: Path):
        from lobster_bridge_local import _list_project_names

        result = _list_project_names(canonical_dir)
        names = [p["name"] for p in result]
        assert names == ["lobster", "transformers"]

    def test_list_project_names_empty(self, empty_canonical_dir: Path):
        from lobster_bridge_local import _list_project_names

        result = _list_project_names(empty_canonical_dir)
        assert result == []

    def test_get_project_context_returns_content(self, canonical_dir: Path):
        from lobster_bridge_local import _get_project_context

        result = _get_project_context(canonical_dir, "lobster")
        assert "Status: active" in result

    def test_get_project_context_returns_available_on_miss(self, canonical_dir: Path):
        from lobster_bridge_local import _get_project_context

        result = _get_project_context(canonical_dir, "govscan")
        assert "No project file for 'govscan'" in result
        assert "lobster" in result

    def test_get_project_context_rejects_traversal(self, canonical_dir: Path):
        from lobster_bridge_local import _get_project_context

        result = _get_project_context(canonical_dir, "../etc/passwd")
        assert "invalid project name" in result


# ===========================================================================
# Tests for local bridge tool dispatch
# ===========================================================================


class TestLocalBridgeDispatch:
    """Tests for the local bridge call_tool dispatch."""

    @pytest.mark.asyncio
    async def test_get_priorities(self, canonical_dir: Path):
        from lobster_bridge_local import call_tool

        with patch("lobster_bridge_local.CANONICAL_DIR", canonical_dir):
            result = await call_tool("get_priorities", {})
        assert "Ship Layer 3" in result[0].text

    @pytest.mark.asyncio
    async def test_get_daily_digest(self, canonical_dir: Path):
        from lobster_bridge_local import call_tool

        with patch("lobster_bridge_local.CANONICAL_DIR", canonical_dir):
            result = await call_tool("get_daily_digest", {})
        assert "All quiet" in result[0].text

    @pytest.mark.asyncio
    async def test_get_handoff(self, canonical_dir: Path):
        from lobster_bridge_local import call_tool

        with patch("lobster_bridge_local.CANONICAL_DIR", canonical_dir):
            result = await call_tool("get_handoff", {})
        assert "Lobster is running" in result[0].text

    @pytest.mark.asyncio
    async def test_get_project_context(self, canonical_dir: Path):
        from lobster_bridge_local import call_tool

        with patch("lobster_bridge_local.CANONICAL_DIR", canonical_dir):
            result = await call_tool("get_project_context", {"project": "transformers"})
        assert "Status: planning" in result[0].text

    @pytest.mark.asyncio
    async def test_list_projects(self, canonical_dir: Path):
        from lobster_bridge_local import call_tool

        with patch("lobster_bridge_local.CANONICAL_DIR", canonical_dir):
            result = await call_tool("list_projects", {})
        parsed = json.loads(result[0].text)
        names = [p["name"] for p in parsed]
        assert names == ["lobster", "transformers"]

    @pytest.mark.asyncio
    async def test_unknown_tool(self, canonical_dir: Path):
        from lobster_bridge_local import call_tool

        with patch("lobster_bridge_local.CANONICAL_DIR", canonical_dir):
            result = await call_tool("nonexistent_tool", {})
        assert "Unknown tool" in result[0].text

    @pytest.mark.asyncio
    async def test_list_projects_empty(self, empty_canonical_dir: Path):
        from lobster_bridge_local import call_tool

        with patch("lobster_bridge_local.CANONICAL_DIR", empty_canonical_dir):
            result = await call_tool("list_projects", {})
        assert "No project files found" in result[0].text


# ===========================================================================
# Tests for local bridge list_tools
# ===========================================================================


class TestLocalBridgeListTools:
    """Tests for the local bridge list_tools."""

    @pytest.mark.asyncio
    async def test_lists_all_five_tools(self):
        from lobster_bridge_local import list_tools

        tools = await list_tools()
        tool_names = {t.name for t in tools}
        # Core tools that must always be present; additional tools may be added.
        required_tools = {
            "get_priorities",
            "get_project_context",
            "get_daily_digest",
            "list_projects",
            "get_handoff",
        }
        assert required_tools.issubset(tool_names), (
            f"Missing expected tools: {required_tools - tool_names}. "
            f"Found: {tool_names}"
        )

    @pytest.mark.asyncio
    async def test_get_project_context_requires_project(self):
        from lobster_bridge_local import list_tools

        tools = await list_tools()
        ctx_tool = next(t for t in tools if t.name == "get_project_context")
        assert "project" in ctx_tool.inputSchema.get("required", [])
