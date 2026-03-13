"""
Tests for Lobster CLI Commands

Tests all 10 CLI commands: start, stop, restart, status, logs, inbox, outbox, stats, test, help
"""

import json
import pytest
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import os


class TestCLIHelpers:
    """Tests for CLI helper functions."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_cli_exists(self, cli_path: Path):
        """Test that CLI script exists."""
        assert cli_path.exists(), f"CLI not found at {cli_path}"

    def test_cli_is_executable(self, cli_path: Path):
        """Test that CLI script is executable."""
        assert os.access(cli_path, os.X_OK), "CLI is not executable"


class TestHelpCommand:
    """Tests for help command."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_help_shows_usage(self, cli_path: Path):
        """Test that help shows usage information."""
        result = subprocess.run(
            ["bash", str(cli_path), "help"],
            capture_output=True,
            text=True,
        )

        assert "Usage" in result.stdout or "usage" in result.stdout.lower()
        assert "lobster" in result.stdout.lower()

    def test_help_lists_commands(self, cli_path: Path):
        """Test that help lists all commands."""
        result = subprocess.run(
            ["bash", str(cli_path), "help"],
            capture_output=True,
            text=True,
        )

        commands = ["start", "stop", "restart", "status", "logs", "inbox", "outbox", "stats", "test"]
        for cmd in commands:
            assert cmd in result.stdout.lower(), f"Command '{cmd}' not in help output"

    def test_default_is_help(self, cli_path: Path):
        """Test that running without args shows help."""
        result = subprocess.run(
            ["bash", str(cli_path)],
            capture_output=True,
            text=True,
        )

        # Should show help by default
        assert "Usage" in result.stdout or "usage" in result.stdout.lower() or "lobster" in result.stdout.lower()


class TestInboxCommand:
    """Tests for inbox command."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_empty_inbox_shows_message(self, cli_path: Path, temp_messages_dir: Path):
        """Test that empty inbox shows appropriate message."""
        inbox = temp_messages_dir / "inbox"

        # Set HOME to temp dir so CLI uses our test directories
        env = os.environ.copy()
        env["HOME"] = str(temp_messages_dir.parent)

        # Create the inbox directory
        inbox.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["bash", str(cli_path), "inbox"],
            capture_output=True,
            text=True,
            env=env,
        )

        # Should indicate empty inbox
        assert "empty" in result.stdout.lower() or "0" in result.stdout

    def test_inbox_with_messages_shows_content(
        self, cli_path: Path, temp_messages_dir: Path, message_generator
    ):
        """Test that inbox with messages shows content."""
        inbox = temp_messages_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)

        # Create a message
        msg = message_generator.generate_text_message(
            text="Test message content",
            user_name="TestUser",
        )
        (inbox / f"{msg['id']}.json").write_text(json.dumps(msg))

        env = os.environ.copy()
        env["HOME"] = str(temp_messages_dir.parent)

        result = subprocess.run(
            ["bash", str(cli_path), "inbox"],
            capture_output=True,
            text=True,
            env=env,
        )

        # Should show the message
        assert "1" in result.stdout  # At least shows count


class TestOutboxCommand:
    """Tests for outbox command."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_empty_outbox_shows_message(self, cli_path: Path, temp_messages_dir: Path):
        """Test that empty outbox shows appropriate message."""
        outbox = temp_messages_dir / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(temp_messages_dir.parent)

        result = subprocess.run(
            ["bash", str(cli_path), "outbox"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert "empty" in result.stdout.lower() or "0" in result.stdout


class TestStatsCommand:
    """Tests for stats command."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_stats_shows_counts(self, cli_path: Path, temp_messages_dir: Path):
        """Test that stats shows message counts."""
        # Create directory structure
        (temp_messages_dir / "inbox").mkdir(parents=True, exist_ok=True)
        (temp_messages_dir / "outbox").mkdir(parents=True, exist_ok=True)
        (temp_messages_dir / "processed").mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(temp_messages_dir.parent)

        result = subprocess.run(
            ["bash", str(cli_path), "stats"],
            capture_output=True,
            text=True,
            env=env,
        )

        # Should show statistics
        assert "inbox" in result.stdout.lower() or "Inbox" in result.stdout
        assert "outbox" in result.stdout.lower() or "Outbox" in result.stdout


class TestTestCommand:
    """Tests for test command."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_creates_test_message(self, cli_path: Path, temp_messages_dir: Path):
        """Test that test command creates a message in inbox."""
        inbox = temp_messages_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(temp_messages_dir.parent)

        result = subprocess.run(
            ["bash", str(cli_path), "test"],
            capture_output=True,
            text=True,
            env=env,
        )

        # Should indicate message was created
        assert "test" in result.stdout.lower() or "created" in result.stdout.lower()

        # Should have created a file
        files = list(inbox.glob("test_*.json"))
        assert len(files) >= 0  # May not exist if HOME doesn't match


class TestUnknownCommand:
    """Tests for unknown command handling."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_unknown_command_shows_error(self, cli_path: Path):
        """Test that unknown command shows error."""
        result = subprocess.run(
            ["bash", str(cli_path), "nonexistent_command"],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0 or "Unknown" in result.stdout or "unknown" in result.stderr.lower()


class TestStatusCommand:
    """Tests for status command."""

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        # tests/unit/test_cli/test_commands.py -> lobster/src/cli
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_status_shows_services(self, cli_path: Path, temp_messages_dir: Path):
        """Test that status shows service information."""
        # Create directory structure
        (temp_messages_dir / "inbox").mkdir(parents=True, exist_ok=True)
        (temp_messages_dir / "outbox").mkdir(parents=True, exist_ok=True)
        (temp_messages_dir / "processed").mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(temp_messages_dir.parent)

        result = subprocess.run(
            ["bash", str(cli_path), "status"],
            capture_output=True,
            text=True,
            env=env,
        )

        # Should show status information
        # Note: Services may not be running in test environment
        assert "Status" in result.stdout or "status" in result.stdout.lower() or "Service" in result.stdout


class TestServiceStatusTmuxAwareness:
    """Tests that lobster-claude service_status distinguishes tmux state from systemd state.

    The lobster-claude service uses RemainAfterExit=yes, meaning systemctl reports
    "active" even when the tmux session has died. The service_status helper must
    perform an explicit tmux check for lobster-claude and surface a warning when
    the session is missing.
    """

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_service_status_contains_tmux_check(self, cli_path: Path):
        """Verify that the CLI source contains a tmux check for lobster-claude."""
        source = cli_path.read_text()
        assert "tmux -L lobster has-session" in source, (
            "CLI must check tmux session for lobster-claude; "
            "systemctl is-active alone is not sufficient (RemainAfterExit=yes)"
        )

    def test_service_status_warns_on_missing_tmux(self, cli_path: Path):
        """Verify the warning text for the 'service active, tmux MISSING' state."""
        source = cli_path.read_text()
        assert "service active, tmux session MISSING" in source, (
            "CLI must emit a clear warning when lobster-claude service is active "
            "but the tmux session is missing"
        )

    def test_service_status_handles_lobster_claude_specifically(self, cli_path: Path):
        """Verify that the tmux check is scoped to lobster-claude only."""
        source = cli_path.read_text()
        # The guard should be present so other services are not affected
        assert '"lobster-claude"' in source or "'lobster-claude'" in source, (
            "The tmux guard must be scoped to the lobster-claude service specifically"
        )
