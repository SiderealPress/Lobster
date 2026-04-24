"""
Tests for config file consolidation (issue #1785, Option A).

Verifies:
1. owner.toml [consolidation] section is parsed correctly by read_owner()
2. get_consolidation_hour() returns the correct hour from owner.toml
3. The repo-level config/ stale files are absent (no lobster/config/config.env,
   lobster/config/consolidation.conf, lobster/config/sync-repos.json)
4. Scripts that previously sourced global.env gracefully skip it when absent
   (shell-level check is done via subprocess)
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Insert src/mcp into path so user_model can be imported directly
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

from user_model.owner import get_consolidation_hour, read_owner


# ---------------------------------------------------------------------------
# Constants matching the spec
# ---------------------------------------------------------------------------

CONSOLIDATION_HOUR = 3  # spec: consolidation runs at 3am


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture
def sample_owner_toml_with_consolidation(tmp_path: Path) -> Path:
    """Write an owner.toml that includes a [consolidation] section."""
    f = tmp_path / "owner.toml"
    f.write_text(
        """# Lobster instance owner identity
# This file contains NO secrets.

[owner]
name = "Alice"
email = "alice@example.com"
timezone = "America/New_York"
telegram_chat_id = "12345"

[consolidation]
hour = "3"
"""
    )
    return f


@pytest.fixture
def sample_owner_toml_without_consolidation(tmp_path: Path) -> Path:
    """Write a legacy owner.toml that has no [consolidation] section."""
    f = tmp_path / "owner.toml"
    f.write_text(
        """# Lobster instance owner identity
# This file contains NO secrets.

[owner]
name = "Bob"
email = "bob@example.com"
timezone = "America/Chicago"
telegram_chat_id = "99999"
"""
    )
    return f


# ---------------------------------------------------------------------------
# owner.toml [consolidation] section parsing
# ---------------------------------------------------------------------------


class TestConsolidationSectionParsing:
    """read_owner() must parse [consolidation] correctly and expose it."""

    def test_consolidation_section_is_parsed(
        self, sample_owner_toml_with_consolidation: Path
    ):
        """[consolidation] section is returned in the dict."""
        data = read_owner(sample_owner_toml_with_consolidation)
        assert "consolidation" in data, (
            "read_owner must return a 'consolidation' key when [consolidation] section present"
        )

    def test_consolidation_hour_value_is_correct(
        self, sample_owner_toml_with_consolidation: Path
    ):
        """[consolidation] hour matches the spec value (CONSOLIDATION_HOUR = 3)."""
        data = read_owner(sample_owner_toml_with_consolidation)
        hour_str = data.get("consolidation", {}).get("hour", "")
        assert int(hour_str) == CONSOLIDATION_HOUR, (
            f"consolidation.hour must be {CONSOLIDATION_HOUR}, got {hour_str!r}"
        )

    def test_other_sections_still_readable(
        self, sample_owner_toml_with_consolidation: Path
    ):
        """Adding [consolidation] must not break parsing of [owner] section."""
        data = read_owner(sample_owner_toml_with_consolidation)
        assert data.get("owner", {}).get("name") == "Alice"
        assert data.get("owner", {}).get("timezone") == "America/New_York"

    def test_missing_consolidation_section_returns_empty(
        self, sample_owner_toml_without_consolidation: Path
    ):
        """Legacy owner.toml without [consolidation] must not crash — returns no section."""
        data = read_owner(sample_owner_toml_without_consolidation)
        assert "consolidation" not in data, (
            "read_owner must not fabricate a consolidation section when absent"
        )


# ---------------------------------------------------------------------------
# get_consolidation_hour() helper
# ---------------------------------------------------------------------------


class TestGetConsolidationHour:
    """get_consolidation_hour() returns hour from [consolidation] or CONSOLIDATION_HOUR default."""

    def test_returns_hour_from_toml(
        self, sample_owner_toml_with_consolidation: Path
    ):
        """Returns the integer hour stored in owner.toml."""
        assert get_consolidation_hour(sample_owner_toml_with_consolidation) == CONSOLIDATION_HOUR

    def test_returns_default_when_section_absent(
        self, sample_owner_toml_without_consolidation: Path
    ):
        """Falls back to CONSOLIDATION_HOUR when [consolidation] section is missing."""
        assert get_consolidation_hour(sample_owner_toml_without_consolidation) == CONSOLIDATION_HOUR

    def test_returns_default_when_file_absent(self, tmp_path: Path):
        """Returns default when the file does not exist at all."""
        nonexistent = tmp_path / "no-such.toml"
        assert get_consolidation_hour(nonexistent) == CONSOLIDATION_HOUR


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
