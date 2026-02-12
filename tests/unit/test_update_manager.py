"""
Tests for Lobster Self-Update System (UpdateManager)

Tests check_for_updates, generate_changelog, analyze_compatibility,
and create_upgrade_plan with mocked git commands.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess


class TestCheckForUpdates:
    """Tests for check_for_updates method."""

    def _make_manager(self, tmp_path):
        from src.mcp.update_manager import UpdateManager
        return UpdateManager(repo_path=tmp_path)

    @patch("src.mcp.update_manager.subprocess.run")
    def test_no_updates_available(self, mock_run, tmp_path):
        """Test when local and remote are at the same SHA."""
        same_sha = "abc1234567890"

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "fetch" in cmd:
                result.stdout = ""
            elif "rev-parse" in cmd and "HEAD" in cmd:
                result.stdout = same_sha + "\n"
            elif "rev-parse" in cmd and "origin/main" in cmd:
                result.stdout = same_sha + "\n"
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.check_for_updates()

        assert result["updates_available"] is False
        assert result["local_sha"] == same_sha

    @patch("src.mcp.update_manager.subprocess.run")
    def test_updates_available(self, mock_run, tmp_path):
        """Test when remote is ahead of local."""
        local_sha = "aaa1111"
        remote_sha = "bbb2222"

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "fetch" in cmd:
                result.stdout = ""
            elif "rev-parse" in cmd:
                if "HEAD" in cmd:
                    result.stdout = local_sha + "\n"
                elif "origin/main" in cmd:
                    result.stdout = remote_sha + "\n"
            elif "rev-list" in cmd and "--count" in cmd:
                result.stdout = "3\n"
            elif "log" in cmd and "--oneline" in cmd:
                result.stdout = "ccc333 feat: new feature\nddd444 fix: bug fix\neee555 docs: update readme\n"
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.check_for_updates()

        assert result["updates_available"] is True
        assert result["local_sha"] == local_sha
        assert result["remote_sha"] == remote_sha
        assert result["commits_behind"] == 3
        assert len(result["commit_log"]) == 3


class TestGenerateChangelog:
    """Tests for generate_changelog method."""

    def _make_manager(self, tmp_path):
        from src.mcp.update_manager import UpdateManager
        return UpdateManager(repo_path=tmp_path)

    @patch("src.mcp.update_manager.subprocess.run")
    def test_categorizes_features(self, mock_run, tmp_path):
        """Test that feat commits are categorized as features."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "log" in cmd:
                result.stdout = "abc123 feat: add new tool (Drew, 2 hours ago)\ndef456 fix: repair inbox (Drew, 1 hour ago)\n"
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        changelog = mgr.generate_changelog(from_sha="old123")

        assert "## Changelog" in changelog
        assert "### New Features" in changelog
        assert "### Bug Fixes" in changelog
        assert "feat: add new tool" in changelog
        assert "fix: repair inbox" in changelog

    @patch("src.mcp.update_manager.subprocess.run")
    def test_no_changes(self, mock_run, tmp_path):
        """Test changelog when there are no changes."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "log" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        changelog = mgr.generate_changelog(from_sha="abc123")

        assert changelog == "No changes."

    @patch("src.mcp.update_manager.subprocess.run")
    def test_other_category(self, mock_run, tmp_path):
        """Test commits that are neither features nor fixes."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "log" in cmd:
                result.stdout = "abc123 chore: update deps (Drew, 1 hour ago)\n"
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        changelog = mgr.generate_changelog(from_sha="old123")

        assert "### Other Changes" in changelog
        assert "chore: update deps" in changelog


class TestAnalyzeCompatibility:
    """Tests for analyze_compatibility method."""

    def _make_manager(self, tmp_path):
        from src.mcp.update_manager import UpdateManager
        return UpdateManager(repo_path=tmp_path)

    @patch("src.mcp.update_manager.subprocess.run")
    def test_safe_update_no_breaking_changes(self, mock_run, tmp_path):
        """Test compatibility with no breaking changes."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "src/mcp/update_manager.py\nREADME.md\n"
            elif "status" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.analyze_compatibility(from_sha="abc123")

        assert result["safe_to_update"] is True
        assert len(result["issues"]) == 0
        assert result["recommendation"] == "auto-update"

    @patch("src.mcp.update_manager.subprocess.run")
    def test_dependency_change_detected(self, mock_run, tmp_path):
        """Test that requirements.txt change is flagged."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "requirements.txt\nsrc/mcp/update_manager.py\n"
            elif "status" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.analyze_compatibility(from_sha="abc123")

        assert any("Dependencies changed" in issue for issue in result["issues"])

    @patch("src.mcp.update_manager.subprocess.run")
    def test_schema_change_marks_unsafe(self, mock_run, tmp_path):
        """Test that schema changes mark update as unsafe."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "db/migration_001.sql\n"
            elif "status" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.analyze_compatibility(from_sha="abc123")

        assert result["safe_to_update"] is False
        assert any("schema" in issue.lower() for issue in result["issues"])
        assert result["recommendation"] == "manual review needed"

    @patch("src.mcp.update_manager.subprocess.run")
    def test_local_conflicting_changes(self, mock_run, tmp_path):
        """Test that conflicting local changes are detected."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "src/mcp/inbox_server.py\n"
            elif "status" in cmd:
                result.stdout = " M src/mcp/inbox_server.py\n"
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.analyze_compatibility(from_sha="abc123")

        assert result["safe_to_update"] is False
        assert any("conflict" in issue.lower() for issue in result["issues"])

    @patch("src.mcp.update_manager.subprocess.run")
    def test_mcp_server_change_warns(self, mock_run, tmp_path):
        """Test that MCP server changes generate warnings."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "src/mcp/inbox_server.py\n"
            elif "status" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.analyze_compatibility(from_sha="abc123")

        assert any("MCP server" in w for w in result["warnings"])

    @patch("src.mcp.update_manager.subprocess.run")
    def test_script_change_warns(self, mock_run, tmp_path):
        """Test that scripts/ changes generate warnings."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "rev-parse" in cmd:
                result.stdout = "abc123\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "scripts/health-check.sh\n"
            elif "status" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        result = mgr.analyze_compatibility(from_sha="abc123")

        assert any("Script/cron" in w for w in result["warnings"])


class TestCreateUpgradePlan:
    """Tests for create_upgrade_plan method."""

    def _make_manager(self, tmp_path):
        from src.mcp.update_manager import UpdateManager
        return UpdateManager(repo_path=tmp_path)

    @patch("src.mcp.update_manager.subprocess.run")
    def test_up_to_date(self, mock_run, tmp_path):
        """Test plan when already up to date."""
        same_sha = "abc1234567890"

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "fetch" in cmd:
                result.stdout = ""
            elif "rev-parse" in cmd:
                result.stdout = same_sha + "\n"
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        plan = mgr.create_upgrade_plan()

        assert plan["action"] == "none"
        assert "up to date" in plan["message"].lower()

    @patch("src.mcp.update_manager.subprocess.run")
    def test_safe_auto_plan(self, mock_run, tmp_path):
        """Test plan for a safe auto-update scenario."""

        def _has_arg(cmd, substr):
            """Check if any argument in cmd list contains substr."""
            return any(substr in arg for arg in cmd)

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "fetch" in cmd:
                result.stdout = ""
            elif "rev-parse" in cmd:
                if "HEAD" in cmd:
                    result.stdout = "aaa111\n"
                elif "origin/main" in cmd:
                    result.stdout = "bbb222\n"
            elif "rev-list" in cmd and "--count" in cmd:
                result.stdout = "2\n"
            elif "log" in cmd and "--oneline" in cmd:
                result.stdout = "ccc333 feat: new feature\nddd444 docs: update readme\n"
            elif "log" in cmd and _has_arg(cmd, "--format"):
                result.stdout = "ccc333 feat: new feature (Drew, 1h ago)\nddd444 docs: update readme (Drew, 2h ago)\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "src/mcp/update_manager.py\nREADME.md\n"
            elif "status" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        plan = mgr.create_upgrade_plan()

        assert plan["action"] == "auto"
        assert plan["commits_behind"] == 2
        assert "## Changelog" in plan["changelog"]
        assert plan["compatibility"]["safe_to_update"] is True
        assert len(plan["steps"]) > 0
        assert any("Pull" in s for s in plan["steps"])

    @patch("src.mcp.update_manager.subprocess.run")
    def test_manual_plan_with_breaking_changes(self, mock_run, tmp_path):
        """Test plan when breaking changes are detected."""

        def _has_arg(cmd, substr):
            return any(substr in arg for arg in cmd)

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "fetch" in cmd:
                result.stdout = ""
            elif "rev-parse" in cmd:
                if "HEAD" in cmd:
                    result.stdout = "aaa111\n"
                elif "origin/main" in cmd:
                    result.stdout = "bbb222\n"
            elif "rev-list" in cmd and "--count" in cmd:
                result.stdout = "1\n"
            elif "log" in cmd and "--oneline" in cmd:
                result.stdout = "ccc333 feat: add migration\n"
            elif "log" in cmd and _has_arg(cmd, "--format"):
                result.stdout = "ccc333 feat: add migration (Drew, 1h ago)\n"
            elif "diff" in cmd and "--name-only" in cmd:
                result.stdout = "db/migration_002.sql\n"
            elif "status" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = side_effect
        mgr = self._make_manager(tmp_path)
        plan = mgr.create_upgrade_plan()

        assert plan["action"] == "manual"
        assert plan["compatibility"]["safe_to_update"] is False
        assert any("rollback" in s.lower() for s in plan["steps"])
