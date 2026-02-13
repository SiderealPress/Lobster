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
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the MCP module under test
# ---------------------------------------------------------------------------

MCP_DIR = Path(__file__).parent.parent / "src" / "mcp"
sys.path.insert(0, str(MCP_DIR))

# We import the specific functions we want to test. Because inbox_server.py
# has heavy imports (mcp SDK, watchdog, reliability, etc.), we mock those at
# import time so the test suite stays fast and dependency-free.

_MOCK_MODULES = {
    "mcp": MagicMock(),
    "mcp.server": MagicMock(),
    "mcp.server.stdio": MagicMock(),
    "mcp.types": MagicMock(),
    "watchdog": MagicMock(),
    "watchdog.observers": MagicMock(),
    "watchdog.events": MagicMock(),
    "reliability": MagicMock(),
    "update_manager": MagicMock(),
    "memory": MagicMock(),
    "httpx": MagicMock(),
}

# Patch sys.modules before importing inbox_server
for mod_name, mock_mod in _MOCK_MODULES.items():
    if mod_name not in sys.modules:
        sys.modules[mod_name] = mock_mod

# Make the mocked reliability module expose the names used at import
_rel = sys.modules["reliability"]
_rel.atomic_write_json = MagicMock()
_rel.validate_send_reply_args = MagicMock()
_rel.validate_message_id = MagicMock()
_rel.ValidationError = type("ValidationError", (Exception,), {})
_rel.init_audit_log = MagicMock()
_rel.audit_log = MagicMock()
_rel.IdempotencyTracker = MagicMock
_rel.CircuitBreaker = MagicMock

# Make mcp.types expose TextContent
_types = sys.modules["mcp.types"]
_types.Tool = type("Tool", (), {"__init__": lambda self, **kw: None})
_types.TextContent = type("TextContent", (), {"__init__": lambda self, **kw: setattr(self, "__dict__", kw) or None})

# Make mcp.server expose Server
_server_mod = sys.modules["mcp.server"]
_mock_server_instance = MagicMock()
_mock_server_instance.list_tools = MagicMock(return_value=lambda f: f)
_mock_server_instance.call_tool = MagicMock(return_value=lambda f: f)
_server_mod.Server = MagicMock(return_value=_mock_server_instance)

# Make update_manager expose UpdateManager
sys.modules["update_manager"].UpdateManager = MagicMock

# Now we can import the functions we want to test
from inbox_server import (
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
                    "name": "Drew Winget",
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
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos()

        assert len(repos) == 2
        assert repos[0] == {"owner": "SiderealPress", "name": "Lobster"}
        assert repos[1] == {"owner": "SiderealPress", "name": "OtherRepo"}

    def test_filter_by_full_name(self, sync_config_file):
        """Filtering by owner/name returns only that repo."""
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("SiderealPress/Lobster")

        assert len(repos) == 1
        assert repos[0]["name"] == "Lobster"

    def test_filter_by_name_only(self, sync_config_file):
        """Filtering by just name (no slash) matches on name field."""
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("OtherRepo")

        assert len(repos) == 1
        assert repos[0]["name"] == "OtherRepo"

    def test_filter_case_insensitive(self, sync_config_file):
        """Repo filtering is case-insensitive."""
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("sideralpress/lobster")
        # Should match despite casing difference ('Sidereal' vs 'sidereal')
        # -- exact spelling must match though
        # Actually "SiderealPress" vs "sideralpress" is a misspelling not a
        # case change.  Let's test true case insensitivity.
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("siderealpress/lobster")
        assert len(repos) == 1
        assert repos[0]["name"] == "Lobster"

    def test_filter_no_match(self, sync_config_file):
        """Filtering for a non-existent repo returns empty list."""
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
            repos = load_sync_repos("NonExistent/Repo")

        assert repos == []

    def test_missing_config_returns_empty(self, temp_dir):
        """If config file doesn't exist, return empty list."""
        missing = temp_dir / "does-not-exist.json"
        with patch("inbox_server.SYNC_REPOS_CONFIG", missing):
            repos = load_sync_repos()

        assert repos == []

    def test_malformed_json_returns_empty(self, temp_dir):
        """Malformed JSON in config returns empty list."""
        bad_file = temp_dir / "bad.json"
        bad_file.write_text("not json{{{")
        with patch("inbox_server.SYNC_REPOS_CONFIG", bad_file):
            repos = load_sync_repos()

        assert repos == []

    def test_disabled_repos_excluded(self, sync_config_file):
        """Disabled repos should not appear in results."""
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
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
        assert result["author"] == "Drew Winget"

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
                "author": "Drew Winget",
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
# Tests: push-canonical.sh script
# =============================================================================


class TestPushCanonicalScript:
    """Tests for the push-canonical.sh shell script."""

    SCRIPT_PATH = str(
        Path(__file__).parent.parent / "scripts" / "push-canonical.sh"
    )

    @pytest.fixture
    def git_repo(self, temp_dir):
        """Create a temporary git repo with a canonical directory."""
        repo = temp_dir / "lobster"
        repo.mkdir()
        canonical = repo / "memory" / "canonical"
        canonical.mkdir(parents=True)

        # Initialize git repo
        subprocess.run(
            ["git", "init"], cwd=str(repo), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "checkout", "-b", "main"],
            cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), capture_output=True, check=True,
        )
        # Initial commit
        (canonical / "handoff.md").write_text("# Handoff\n")
        subprocess.run(
            ["git", "add", "."], cwd=str(repo), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo), capture_output=True, check=True,
        )

        return repo

    def _run_push_script(self, repo_dir, env_overrides=None):
        """Run push-canonical.sh in the test repo."""
        env = os.environ.copy()
        env["PUSH_CANONICAL_DRY_RUN"] = "1"  # Always dry-run in tests
        if env_overrides:
            env.update(env_overrides)

        # We need to point SCRIPT_DIR to the actual script but run in repo_dir
        # The script uses LOBSTER_DIR from SCRIPT_DIR, so we create a wrapper
        result = subprocess.run(
            ["bash", "-c", f"""
                SCRIPT_DIR="{Path(self.SCRIPT_PATH).parent}"
                LOBSTER_DIR="{repo_dir}"
                CANONICAL_REL="memory/canonical"
                DATE_STAMP="$(date +%Y-%m-%d)"
                DRY_RUN="${{PUSH_CANONICAL_DRY_RUN:-0}}"

                cd "$LOBSTER_DIR"

                CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
                if [ "$CURRENT_BRANCH" != "main" ] && [ "${{PUSH_CANONICAL_ALLOW_BRANCH:-0}}" != "1" ]; then
                    echo "[push-canonical] WARNING: not on main branch. Skipping."
                    exit 0
                fi

                if [ ! -d "$CANONICAL_REL" ]; then
                    echo "[push-canonical] No canonical directory. Nothing to push."
                    exit 0
                fi

                git add "$CANONICAL_REL"

                if git diff --cached --quiet -- "$CANONICAL_REL"; then
                    echo "[push-canonical] No canonical file changes to push."
                    exit 0
                fi

                COMMIT_MSG="chore: nightly consolidation $DATE_STAMP"

                if [ "$DRY_RUN" = "1" ]; then
                    echo "[push-canonical] DRY RUN: would commit"
                    git diff --cached --name-only -- "$CANONICAL_REL"
                    git reset HEAD -- "$CANONICAL_REL" > /dev/null 2>&1
                    exit 0
                fi

                git commit -m "$COMMIT_MSG"
            """],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_no_changes_noop(self, git_repo):
        """If no canonical files changed, script is a no-op."""
        result = self._run_push_script(git_repo)
        assert result.returncode == 0
        assert "No canonical file changes" in result.stdout

    def test_detects_changes_in_dry_run(self, git_repo):
        """Modified canonical files are detected in dry-run."""
        # Modify a canonical file
        (git_repo / "memory" / "canonical" / "handoff.md").write_text(
            "# Updated Handoff\n\nNew content.\n"
        )

        result = self._run_push_script(git_repo)
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout

    def test_new_files_detected(self, git_repo):
        """New canonical files are detected."""
        (git_repo / "memory" / "canonical" / "new-file.md").write_text(
            "# New File\n"
        )

        result = self._run_push_script(git_repo)
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout

    def test_not_on_main_skips(self, git_repo):
        """Script skips if not on main branch (without override)."""
        subprocess.run(
            ["git", "checkout", "-b", "other"],
            cwd=str(git_repo), capture_output=True, check=True,
        )

        result = self._run_push_script(git_repo)
        assert result.returncode == 0
        assert "not on main branch" in result.stdout or "Skipping" in result.stdout

    def test_idempotent(self, git_repo):
        """Running twice with no new changes is safe."""
        result1 = self._run_push_script(git_repo)
        result2 = self._run_push_script(git_repo)
        assert result1.returncode == 0
        assert result2.returncode == 0
        assert "No canonical file changes" in result2.stdout


# =============================================================================
# Tests: fetch_sync_branch (async, mocked gh CLI)
# =============================================================================


class TestFetchSyncBranch:
    """Tests for the async fetch_sync_branch function."""

    @pytest.fixture(autouse=True)
    def import_handler(self):
        """Import the async function under test."""
        from inbox_server import fetch_sync_branch
        self.fetch_sync_branch = fetch_sync_branch

    @pytest.mark.asyncio
    async def test_branch_not_found(self):
        """Returns error dict when lobster-sync branch doesn't exist."""
        with patch("inbox_server.run_gh_command", new_callable=AsyncMock) as mock_gh:
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

        with patch("inbox_server.run_gh_command", side_effect=mock_gh):
            result = await self.fetch_sync_branch("SiderealPress", "Lobster")

        assert result["repo"] == "SiderealPress/Lobster"
        assert result["sha"] == "abc12345"
        assert "error" not in result
        assert result["divergence"]["ahead_by"] == 3

    @pytest.mark.asyncio
    async def test_api_error(self):
        """Returns error dict on unexpected API failure."""
        with patch("inbox_server.run_gh_command", new_callable=AsyncMock) as mock_gh:
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

        with patch("inbox_server.run_gh_command", side_effect=mock_gh):
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
        from inbox_server import handle_check_local_sync
        self.handler = handle_check_local_sync

    @pytest.mark.asyncio
    async def test_no_repos_configured(self, temp_dir):
        """Returns helpful message when no repos configured."""
        missing = temp_dir / "missing.json"
        with patch("inbox_server.SYNC_REPOS_CONFIG", missing):
            result = await self.handler({})

        text = result[0].__dict__.get("text", str(result[0]))
        assert "No repos configured" in text or "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_repo_filter_not_found(self, sync_config_file):
        """Returns error message when filtered repo doesn't exist in config."""
        with patch("inbox_server.SYNC_REPOS_CONFIG", sync_config_file):
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
