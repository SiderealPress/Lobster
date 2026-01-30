"""
Tests for MCP Server Brain Dump Triage Tools

Tests triage_brain_dump, create_action_item, link_action_to_brain_dump,
close_brain_dump, and get_brain_dump_status.
"""

import json
import pytest
from unittest.mock import patch, AsyncMock
import asyncio


class TestTriageBrainDump:
    """Tests for triage_brain_dump tool."""

    @pytest.fixture
    def mock_gh_command(self):
        """Mock the run_gh_command function."""
        async def mock_run(args):
            # Default: success
            return (True, "", "")
        return mock_run

    def test_requires_owner(self):
        """Test that owner is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_triage_brain_dump

            result = asyncio.run(handle_triage_brain_dump({
                "repo": "brain-dumps",
                "issue_number": 42,
                "action_items": []
            }))

            assert "Error" in result[0].text
            assert "owner" in result[0].text.lower()

    def test_requires_repo(self):
        """Test that repo is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_triage_brain_dump

            result = asyncio.run(handle_triage_brain_dump({
                "owner": "testuser",
                "issue_number": 42,
                "action_items": []
            }))

            assert "Error" in result[0].text
            assert "repo" in result[0].text.lower()

    def test_requires_issue_number(self):
        """Test that issue_number is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_triage_brain_dump

            result = asyncio.run(handle_triage_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "action_items": []
            }))

            assert "Error" in result[0].text
            assert "issue_number" in result[0].text.lower()

    def test_successful_triage(self):
        """Test successful triage with action items."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_triage_brain_dump

            result = asyncio.run(handle_triage_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42,
                "action_items": [
                    {"title": "Action 1", "description": "Do something"},
                    {"title": "Action 2"}
                ]
            }))

            assert "triaged" in result[0].text.lower()
            assert "42" in result[0].text
            assert "2 action item" in result[0].text

    def test_triage_with_notes(self):
        """Test triage includes notes when provided."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_triage_brain_dump

            result = asyncio.run(handle_triage_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42,
                "action_items": [{"title": "Test"}],
                "triage_notes": "Matched ProjectX from context"
            }))

            assert "triaged" in result[0].text.lower()


class TestCreateActionItem:
    """Tests for create_action_item tool."""

    def test_requires_owner(self):
        """Test that owner is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_create_action_item

            result = asyncio.run(handle_create_action_item({
                "repo": "brain-dumps",
                "brain_dump_issue": 42,
                "title": "Action item"
            }))

            assert "Error" in result[0].text

    def test_requires_brain_dump_issue(self):
        """Test that brain_dump_issue is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_create_action_item

            result = asyncio.run(handle_create_action_item({
                "owner": "testuser",
                "repo": "brain-dumps",
                "title": "Action item"
            }))

            assert "Error" in result[0].text
            assert "brain_dump_issue" in result[0].text.lower()

    def test_requires_title(self):
        """Test that title is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_create_action_item

            result = asyncio.run(handle_create_action_item({
                "owner": "testuser",
                "repo": "brain-dumps",
                "brain_dump_issue": 42
            }))

            assert "Error" in result[0].text
            assert "title" in result[0].text.lower()

    def test_successful_creation(self):
        """Test successful action item creation."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            # Return issue URL on creation
            mock.return_value = (True, "https://github.com/testuser/brain-dumps/issues/43", "")
            from src.mcp.inbox_server import handle_create_action_item

            result = asyncio.run(handle_create_action_item({
                "owner": "testuser",
                "repo": "brain-dumps",
                "brain_dump_issue": 42,
                "title": "Research OAuth providers",
                "body": "Compare Auth0 and Okta"
            }))

            assert "created" in result[0].text.lower()
            assert "#43" in result[0].text
            assert "42" in result[0].text  # Parent reference

    def test_creation_with_labels(self):
        """Test action item creation with labels."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "https://github.com/testuser/brain-dumps/issues/43", "")
            from src.mcp.inbox_server import handle_create_action_item

            result = asyncio.run(handle_create_action_item({
                "owner": "testuser",
                "repo": "brain-dumps",
                "brain_dump_issue": 42,
                "title": "Research OAuth",
                "labels": ["urgent", "project:auth"]
            }))

            assert "created" in result[0].text.lower()


class TestLinkActionToBrainDump:
    """Tests for link_action_to_brain_dump tool."""

    def test_requires_brain_dump_issue(self):
        """Test that brain_dump_issue is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_link_action_to_brain_dump

            result = asyncio.run(handle_link_action_to_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "action_issue": 43,
                "action_title": "Test"
            }))

            assert "Error" in result[0].text

    def test_requires_action_issue(self):
        """Test that action_issue is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_link_action_to_brain_dump

            result = asyncio.run(handle_link_action_to_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "brain_dump_issue": 42,
                "action_title": "Test"
            }))

            assert "Error" in result[0].text

    def test_successful_linking(self):
        """Test successful linking of action to brain dump."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_link_action_to_brain_dump

            result = asyncio.run(handle_link_action_to_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "brain_dump_issue": 42,
                "action_issue": 43,
                "action_title": "Research OAuth"
            }))

            assert "Linked" in result[0].text
            assert "#43" in result[0].text
            assert "#42" in result[0].text


class TestCloseBrainDump:
    """Tests for close_brain_dump tool."""

    def test_requires_issue_number(self):
        """Test that issue_number is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_close_brain_dump

            result = asyncio.run(handle_close_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "summary": "All done"
            }))

            assert "Error" in result[0].text

    def test_requires_summary(self):
        """Test that summary is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_close_brain_dump

            result = asyncio.run(handle_close_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42
            }))

            assert "Error" in result[0].text
            assert "summary" in result[0].text.lower()

    def test_successful_close(self):
        """Test successful brain dump closure."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_close_brain_dump

            result = asyncio.run(handle_close_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42,
                "summary": "Processed brain dump. Created 2 action items.",
                "action_issues": [43, 44]
            }))

            assert "closed" in result[0].text.lower()
            assert "42" in result[0].text
            assert "2 action item" in result[0].text

    def test_close_without_action_issues(self):
        """Test closing brain dump without action items."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_close_brain_dump

            result = asyncio.run(handle_close_brain_dump({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42,
                "summary": "Reference only - no actions needed."
            }))

            assert "closed" in result[0].text.lower()
            assert "0 action item" in result[0].text


class TestGetBrainDumpStatus:
    """Tests for get_brain_dump_status tool."""

    def test_requires_issue_number(self):
        """Test that issue_number is required."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            mock.return_value = (True, "", "")
            from src.mcp.inbox_server import handle_get_brain_dump_status

            result = asyncio.run(handle_get_brain_dump_status({
                "owner": "testuser",
                "repo": "brain-dumps"
            }))

            assert "Error" in result[0].text

    def test_returns_issue_status(self):
        """Test that issue status is returned correctly."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            # Mock issue data
            issue_data = {
                "title": "Brain dump about auth",
                "state": "open",
                "labels": [{"name": "triaged"}],
                "comments": [
                    {"body": "Action item created: #43: Research OAuth"}
                ]
            }
            mock.return_value = (True, json.dumps(issue_data), "")
            from src.mcp.inbox_server import handle_get_brain_dump_status

            result = asyncio.run(handle_get_brain_dump_status({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42
            }))

            text = result[0].text
            assert "42" in text
            assert "open" in text.lower()
            assert "triaged" in text.lower()

    def test_detects_workflow_status_raw(self):
        """Test that raw status is detected."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            issue_data = {
                "title": "New brain dump",
                "state": "open",
                "labels": [{"name": "raw"}],
                "comments": []
            }
            mock.return_value = (True, json.dumps(issue_data), "")
            from src.mcp.inbox_server import handle_get_brain_dump_status

            result = asyncio.run(handle_get_brain_dump_status({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42
            }))

            assert "raw" in result[0].text.lower()

    def test_detects_workflow_status_completed(self):
        """Test that completed status is detected."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            issue_data = {
                "title": "Processed brain dump",
                "state": "closed",
                "labels": [{"name": "actioned"}],
                "comments": []
            }
            mock.return_value = (True, json.dumps(issue_data), "")
            from src.mcp.inbox_server import handle_get_brain_dump_status

            result = asyncio.run(handle_get_brain_dump_status({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42
            }))

            assert "completed" in result[0].text.lower()

    def test_finds_linked_action_items(self):
        """Test that linked action items are found in comments."""
        with patch("src.mcp.inbox_server.run_gh_command", new_callable=AsyncMock) as mock:
            issue_data = {
                "title": "Brain dump with actions",
                "state": "open",
                "labels": [{"name": "triaged"}],
                "comments": [
                    {"body": "Triage complete"},
                    {"body": "Action item created: #43: Task 1"},
                    {"body": "Action item created: #44: Task 2"}
                ]
            }
            mock.return_value = (True, json.dumps(issue_data), "")
            from src.mcp.inbox_server import handle_get_brain_dump_status

            result = asyncio.run(handle_get_brain_dump_status({
                "owner": "testuser",
                "repo": "brain-dumps",
                "issue_number": 42
            }))

            text = result[0].text
            assert "#43" in text
            assert "#44" in text


class TestLabelManagement:
    """Tests for label creation and management."""

    def test_ensure_label_exists_creates_missing(self):
        """Test that missing labels are created."""
        call_count = 0
        async def mock_run(args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Label doesn't exist
                return (False, "", "not found")
            else:
                # Label created
                return (True, "", "")

        with patch("src.mcp.inbox_server.run_gh_command", side_effect=mock_run):
            from src.mcp.inbox_server import ensure_label_exists

            result = asyncio.run(ensure_label_exists("owner", "repo", "test-label"))

            assert result is True
            assert call_count == 2  # Check + create

    def test_ensure_label_exists_skips_existing(self):
        """Test that existing labels are not recreated."""
        async def mock_run(args):
            # Label exists
            return (True, '{"name": "test-label"}', "")

        with patch("src.mcp.inbox_server.run_gh_command", side_effect=mock_run):
            from src.mcp.inbox_server import ensure_label_exists

            result = asyncio.run(ensure_label_exists("owner", "repo", "test-label"))

            assert result is True
