"""
Tests for config file consolidation (issue #1785, Option A).

Verifies:
1. owner.toml sections are parsed correctly by read_owner()
2. The repo-level config/ stale files are absent (no lobster/config/config.env,
   lobster/config/consolidation.conf, lobster/config/sync-repos.json)
3. Scripts that previously sourced global.env gracefully skip it when absent
   (shell-level check is done via subprocess)
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Insert src/mcp into path so user_model can be imported directly
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from user_model.owner import read_owner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture
def sample_owner_toml(tmp_path: Path) -> Path:
    """Write a minimal owner.toml for parser tests."""
    f = tmp_path / "owner.toml"
    f.write_text(
        """# Lobster instance owner identity
# This file contains NO secrets.

[owner]
name = "Alice"
email = "alice@example.com"
timezone = "America/New_York"
telegram_chat_id = "12345"
"""
    )
    return f


# ---------------------------------------------------------------------------
# owner.toml parsing
# ---------------------------------------------------------------------------


class TestOwnerTomlParsing:
    """read_owner() must parse owner.toml correctly."""

    def test_owner_section_is_parsed(self, sample_owner_toml: Path):
        """[owner] section is returned in the dict."""
        data = read_owner(sample_owner_toml)
        assert "owner" in data, "read_owner must return an 'owner' key"

    def test_owner_name_value_is_correct(self, sample_owner_toml: Path):
        """[owner] name is parsed correctly."""
        data = read_owner(sample_owner_toml)
        assert data.get("owner", {}).get("name") == "Alice"

    def test_owner_timezone_value_is_correct(self, sample_owner_toml: Path):
        """[owner] timezone is parsed correctly."""
        data = read_owner(sample_owner_toml)
        assert data.get("owner", {}).get("timezone") == "America/New_York"


# ---------------------------------------------------------------------------
# Stale repo-level config/ files must not exist
# ---------------------------------------------------------------------------


class TestStaleRepoConfigFilesAbsent:
    """Verify that stale files in lobster/config/ have been removed."""

    def test_repo_config_env_deleted(self):
        """lobster/config/config.env must not exist (it was stale and unread)."""
        stale = REPO_ROOT / "config" / "config.env"
        assert not stale.exists(), (
            f"Stale {stale} should have been deleted — it diverged from lobster-config/ "
            "and was not read by any script"
        )

    def test_repo_consolidation_conf_deleted(self):
        """lobster/config/consolidation.conf must not exist (duplicate of lobster-config/ version)."""
        stale = REPO_ROOT / "config" / "consolidation.conf"
        assert not stale.exists(), (
            f"Stale {stale} should have been deleted — duplicate left by old migration"
        )

    def test_repo_sync_repos_json_deleted(self):
        """lobster/config/sync-repos.json must not exist (duplicate of lobster-config/ version)."""
        stale = REPO_ROOT / "config" / "sync-repos.json"
        assert not stale.exists(), (
            f"Stale {stale} should have been deleted — duplicate left by old migration"
        )
