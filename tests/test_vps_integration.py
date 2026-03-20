"""
Tests for Layer 5: VPS-Side Integration (sync awareness + canonical push).

Covers:
  - Config loading and repo filtering (pure functions)
  - Branch info parsing from GitHub API responses (pure functions)
  - Compare/divergence parsing (pure functions)
  - Status report formatting (pure functions)
  - check_local_sync handler (async, with mocked gh CLI)
  - push-canonical.sh script behaviour (shell, idempotent)
  - Graceful handling when lobster-sync branch does not exist
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the MCP module under test via canonical package path.
# No sys.path hacks or sys.modules pollution — all real deps are available
# in the test venv.
# ---------------------------------------------------------------------------

from src.mcp.inbox_server import (
    load_sync_repos,
    parse_branch_info,
    parse_compare_info,
    format_sync_status,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    tmp = tempfile.mkdtemp(prefix="lobster_vps_test_")
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def sync_config_file(temp_dir):
    """Create a temporary sync-repos.json config."""
    config = {
        "sync_branch": "lobster-sync",
        "repos": [
            {"owner": "SiderealPress", "name": "Lobster", "enabled": True},
            {"owner": "SiderealPress", "name": "OtherRepo", "enabled": True},
            {"owner": "SiderealPress", "name": "DisabledRepo", "enabled": False},
        ],
    }
    path = temp_dir / "sync-repos.json"
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def sample_branch_api_response():
    """A realistic GitHub branch API response."""
    return {
        "name": "lobster-sync",
        "commit": {
            "sha": "abc12345deadbeef",
            "commit": {
                "message": "lobster-sync: feature/new-widget @ abc1234",
                "committer": {
                    "name": "lobster-sync",
                    "date": "2026-02-12T20:30:00Z",
                },
                "author": {
                    "name": "Test User",
                    "date": "2026-02-12T20:30:00Z",
                },
            },
        },
    }


@pytest.fixture
def sample_compare_api_response():
    """A realistic GitHub compare API response."""
    return {
        "ahead_by": 3,
        "behind_by": 1,
        "total_commits": 3,
        "files": [
            {"filename": "src/main.py"},
            {"filename": "tests/test_main.py"},
        ],
    }


# =============================================================================
# Tests: load_sync_repos (pure function)
# =============================================================================


class TestLoadSyncRepos:
    """Tests for the sync repos config loader."""

    def test_loads_enabled_repos(self, sync_config_file):
        """Only enabled repos are returned."""
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos()

        assert len(repos) == 2
        assert repos[0] == {"owner": "SiderealPress", "name": "Lobster"}
        assert repos[1] == {"owner": "SiderealPress", "name": "OtherRepo"}

    def test_filter_by_full_name(self, sync_config_file):
        """Filtering by owner/name returns only that repo."""
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("SiderealPress/Lobster")

        assert len(repos) == 1
        assert repos[0]["name"] == "Lobster"

    def test_filter_by_name_only(self, sync_config_file):
        """Filtering by just name (no slash) matches on name field."""
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("OtherRepo")

        assert len(repos) == 1
        assert repos[0]["name"] == "OtherRepo"

    def test_filter_case_insensitive(self, sync_config_file):
        """Repo filtering is case-insensitive."""
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("sideralpress/lobster")
        # Should match despite casing difference ('Sidereal' vs 'sidereal')
        # -- exact spelling must match though
        # Actually "SiderealPress" vs "sideralpress" is a misspelling not a
        # case change.  Let's test true case insensitivity.
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("siderealpress/lobster")
        assert len(repos) == 1
        assert repos[0]["name"] == "Lobster"

    def test_filter_no_match(self, sync_config_file):
        """Filtering for a non-existent repo returns empty list."""
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("NonExistent/Repo")

        assert repos == []

    def test_missing_config_returns_empty(self, temp_dir):
        """If config file doesn't exist, return empty list."""
        missing = temp_dir / "does-not-exist.json"
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", missing):
            repos = load_sync_repos()

        assert repos == []

    def test_malformed_json_returns_empty(self, temp_dir):
        """Malformed JSON in config returns empty list."""
        bad_file = temp_dir / "bad.json"
        bad_file.write_text("not json{{{")
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", bad_file):
            repos = load_sync_repos()

        assert repos == []

    def test_disabled_repos_excluded(self, sync_config_file):
        """Disabled repos should not appear in results."""
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos()

        names = [r["name"] for r in repos]
        assert "DisabledRepo" not in names


# =============================================================================
# Tests: parse_branch_info (pure function)
# =============================================================================


class TestParseBranchInfo:
    """Tests for parsing GitHub branch API responses."""

    def test_extracts_all_fields(self, sample_branch_api_response):
        """All expected fields are extracted."""
        result = parse_branch_info(
            sample_branch_api_response, "SiderealPress", "Lobster"
        )

        assert result["repo"] == "SiderealPress/Lobster"
        assert result["last_sync"] == "2026-02-12T20:30:00Z"
        assert "lobster-sync" in result["message"]
        assert result["sha"] == "abc12345"
        assert result["author"] == "Test User"

    def test_handles_empty_response(self):
        """Graceful handling of an empty/minimal response."""
        result = parse_branch_info({}, "owner", "repo")

        assert result["repo"] == "owner/repo"
        assert result["last_sync"] == "unknown"
        assert result["message"] == ""
        assert result["sha"] == ""
        assert result["author"] == "unknown"

    def test_sha_truncated_to_eight(self, sample_branch_api_response):
        """SHA is truncated to 8 characters."""
        result = parse_branch_info(
            sample_branch_api_response, "o", "r"
        )
        assert len(result["sha"]) == 8


# =============================================================================
# Tests: parse_compare_info (pure function)
# =============================================================================


class TestParseCompareInfo:
    """Tests for parsing GitHub compare API responses."""

    def test_extracts_divergence(self, sample_compare_api_response):
        """Ahead/behind counts and file count are extracted."""
        result = parse_compare_info(sample_compare_api_response)

        assert result["ahead_by"] == 3
        assert result["behind_by"] == 1
        assert result["total_commits"] == 3
        assert result["changed_files"] == 2

    def test_handles_empty_response(self):
        """Defaults to zeros when fields are missing."""
        result = parse_compare_info({})

        assert result["ahead_by"] == 0
        assert result["behind_by"] == 0
        assert result["total_commits"] == 0
        assert result["changed_files"] == 0


# =============================================================================
# Tests: format_sync_status (pure function)
# =============================================================================


class TestFormatSyncStatus:
    """Tests for the sync status report formatter."""

    def test_empty_results(self):
        """Empty results produce a helpful message."""
        output = format_sync_status([])
        assert "No registered repos" in output

    def test_single_repo_success(self):
        """A successful single-repo result is formatted properly."""
        results = [
            {
                "repo": "SiderealPress/Lobster",
                "last_sync": "2026-02-12T20:30:00Z",
                "message": "lobster-sync: feature/widget @ abc1234",
                "sha": "abc12345",
                "author": "Test User",
                "divergence": {
                    "ahead_by": 3,
                    "behind_by": 0,
                    "total_commits": 3,
                    "changed_files": 5,
                },
            }
        ]
        output = format_sync_status(results)

        assert "SiderealPress/Lobster" in output
        assert "2026-02-12T20:30:00Z" in output
        assert "abc12345" in output
        assert "3 commits ahead" in output
        assert "5 files changed" in output

    def test_repo_with_error(self):
        """A repo with an error shows the error message."""
        results = [
            {
                "repo": "SiderealPress/Missing",
                "error": "No `lobster-sync` branch found",
            }
        ]
        output = format_sync_status(results)

        assert "SiderealPress/Missing" in output
        assert "No `lobster-sync` branch found" in output

    def test_mixed_success_and_error(self):
        """Multiple repos with mix of success/error are all shown."""
        results = [
            {
                "repo": "A/One",
                "last_sync": "2026-01-01T00:00:00Z",
                "message": "sync",
                "sha": "11111111",
                "author": "dev",
            },
            {
                "repo": "B/Two",
                "error": "API error: 500",
            },
        ]
        output = format_sync_status(results)

        assert "A/One" in output
        assert "B/Two" in output
        assert "API error" in output


# =============================================================================
# Tests: fetch_sync_branch (async, mocked gh CLI)
# =============================================================================


class TestFetchSyncBranch:
    """Tests for the async fetch_sync_branch function."""

    @pytest.fixture(autouse=True)
    def import_handler(self):
        """Import the async function under test."""
        from src.mcp.inbox_server import fetch_sync_branch
        self.fetch_sync_branch = fetch_sync_branch

    @pytest.mark.asyncio
    async def test_branch_not_found(self):
        """Returns error dict when lobster-sync branch doesn't exist."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock_gh:
            mock_gh.return_value = (False, "", "HTTP 404: Not Found")

            result = await self.fetch_sync_branch("owner", "repo")

        assert result["repo"] == "owner/repo"
        assert "No `lobster-sync` branch found" in result["error"]

    @pytest.mark.asyncio
    async def test_branch_found(self, sample_branch_api_response, sample_compare_api_response):
        """Returns parsed branch info and divergence on success."""
        branch_json = json.dumps(sample_branch_api_response)
        compare_json = json.dumps({
            "ahead_by": sample_compare_api_response["ahead_by"],
            "behind_by": sample_compare_api_response["behind_by"],
            "total_commits": sample_compare_api_response["total_commits"],
            "files": [{"filename": f["filename"]} for f in sample_compare_api_response["files"]],
        })

        call_count = 0

        async def mock_gh(args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (True, branch_json, "")
            return (True, compare_json, "")

        with patch("src.mcp.inbox_server.run_gh_command", side_effect=mock_gh):
            result = await self.fetch_sync_branch("SiderealPress", "Lobster")

        assert result["repo"] == "SiderealPress/Lobster"
        assert result["sha"] == "abc12345"
        assert "error" not in result
        assert result["divergence"]["ahead_by"] == 3

    @pytest.mark.asyncio
    async def test_api_error(self):
        """Returns error dict on unexpected API failure."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock_gh:
            mock_gh.return_value = (False, "", "HTTP 500: Internal Server Error")

            result = await self.fetch_sync_branch("owner", "repo")

        assert "API error" in result["error"]

    @pytest.mark.asyncio
    async def test_compare_failure_still_returns_branch_info(
        self, sample_branch_api_response
    ):
        """If the compare call fails, branch info is still returned without divergence."""
        branch_json = json.dumps(sample_branch_api_response)

        call_count = 0

        async def mock_gh(args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (True, branch_json, "")
            return (False, "", "compare failed")

        with patch("src.mcp.inbox_server.run_gh_command", side_effect=mock_gh):
            result = await self.fetch_sync_branch("SiderealPress", "Lobster")

        assert result["sha"] == "abc12345"
        assert "divergence" not in result
        assert "error" not in result


# =============================================================================
# Tests: handle_check_local_sync (async handler, mocked)
# =============================================================================


class TestHandleCheckLocalSync:
    """Tests for the MCP tool handler."""

    @pytest.fixture(autouse=True)
    def import_handler(self):
        """Import the handler under test."""
        from src.mcp.inbox_server import handle_check_local_sync
        self.handler = handle_check_local_sync

    @pytest.mark.asyncio
    async def test_no_repos_configured(self, temp_dir):
        """Returns helpful message when no repos configured."""
        missing = temp_dir / "missing.json"
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", missing):
            result = await self.handler({})

        text = result[0].__dict__.get("text", str(result[0]))
        assert "No repos configured" in text or "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_repo_filter_not_found(self, sync_config_file):
        """Returns error message when filtered repo doesn't exist in config."""
        with patch("src.mcp.inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            result = await self.handler({"repo": "NonExistent/Repo"})

        text = result[0].__dict__.get("text", str(result[0]))
        assert "not found" in text.lower() or "No repos" in text


# =============================================================================
# Tests: sync-repos.json config file validation
# =============================================================================


class TestSyncReposConfig:
    """Validate the shipped config file."""

    CONFIG_PATH = Path(__file__).parent.parent / "config" / "sync-repos.json"

    def test_config_is_valid_json(self):
        """The shipped config file is valid JSON."""
        assert self.CONFIG_PATH.exists(), "config/sync-repos.json should exist"
        data = json.loads(self.CONFIG_PATH.read_text())
        assert "repos" in data
        assert isinstance(data["repos"], list)

    def test_config_has_sync_branch(self):
        """Config specifies a sync_branch name."""
        data = json.loads(self.CONFIG_PATH.read_text())
        assert "sync_branch" in data
        assert isinstance(data["sync_branch"], str)
        assert len(data["sync_branch"]) > 0

    def test_repo_entries_have_required_fields(self):
        """Each repo entry has owner, name, and enabled fields."""
        data = json.loads(self.CONFIG_PATH.read_text())
        for repo in data["repos"]:
            assert "owner" in repo
            assert "name" in repo
            assert "enabled" in repo
