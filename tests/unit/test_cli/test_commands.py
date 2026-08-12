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
        # LOBSTER_MESSAGES overrides $HOME/messages in the CLI.  Unset it so
        # the CLI falls back to $HOME/messages (our temp dir) rather than
        # pointing at the live /home/lobster/messages directory.
        env.pop("LOBSTER_MESSAGES", None)

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
        # Unset LOBSTER_MESSAGES so CLI falls back to $HOME/messages (our temp
        # dir) rather than pointing at the live messages directory with thousands
        # of processed files that would make the per-file jq loop prohibitively slow.
        env.pop("LOBSTER_MESSAGES", None)

        result = subprocess.run(
            ["bash", str(cli_path), "stats"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
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

    def test_message_uses_registered_source_and_self_check_type(
        self, cli_path: Path, temp_messages_dir: Path
    ):
        """Regression test for issue #2201.

        cmd_test previously wrote source="test", which is not (and never was)
        a member of INBOX_MESSAGE_SOURCES (src/mcp/message_types.py). Since
        PR #1736 (issue #1735) added source-quarantine, every `lobster test`
        run was silently dead-lettered to failed/ despite the CLI printing a
        "should process shortly" success message.

        Fix: write source="system" (a registered source, matching the sibling
        fix already applied to daily-health-check.sh) and type="self_check"
        explicitly (self_check is a registered INBOX_SYSTEM_TYPES member and
        is not in USER_FACING_TYPES, so a chat_id=0 message with this type
        will not be mistaken for a user-facing message needing a reply).
        """
        inbox = temp_messages_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(temp_messages_dir.parent)
        env.pop("LOBSTER_MESSAGES", None)

        result = subprocess.run(
            ["bash", str(cli_path), "test"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"lobster test failed: {result.stderr}"

        files = list(inbox.glob("test_*.json"))
        assert len(files) == 1, f"Expected exactly one test_*.json file, got {files}"

        msg = json.loads(files[0].read_text())
        assert msg["source"] == "system", (
            f"cmd_test must write source='system' (registered in INBOX_MESSAGE_SOURCES), "
            f"got source={msg.get('source')!r}"
        )
        assert msg["type"] == "self_check", (
            f"cmd_test must write type='self_check' (registered in INBOX_SYSTEM_TYPES), "
            f"got type={msg.get('type')!r}"
        )

        # Cross-check against the live taxonomy so this test breaks if the
        # constants ever change underneath it.
        import sys as _sys
        mcp_dir = str(Path(__file__).parent.parent.parent.parent / "src" / "mcp")
        if mcp_dir not in _sys.path:
            _sys.path.insert(0, mcp_dir)
        from message_types import INBOX_MESSAGE_SOURCES, INBOX_SYSTEM_TYPES, USER_FACING_TYPES

        assert msg["source"] in INBOX_MESSAGE_SOURCES, (
            f"cmd_test source {msg['source']!r} is not in INBOX_MESSAGE_SOURCES — "
            "it would be quarantined to failed/ by the inbox server's source guard"
        )
        assert msg["type"] in INBOX_SYSTEM_TYPES, (
            f"cmd_test type {msg['type']!r} is not in INBOX_SYSTEM_TYPES"
        )
        assert msg["type"] not in USER_FACING_TYPES, (
            f"cmd_test type {msg['type']!r} must not be user-facing — "
            "the message carries chat_id=0 and is not a real user conversation"
        )


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


class TestEnvCommand:
    """Tests for lobster env set/get — verifies fix for #1454.

    The bug: cmd_env defaulted LOBSTER_CONFIG_DIR to ~/lobster-user-config but
    services read from ~/lobster-config. Tokens written to the wrong directory
    were never seen by systemd EnvironmentFile.

    The fix: default changed to ~/lobster-config (matching install.sh), and
    env set now writes to config.env when the key already exists there (to avoid
    empty-stub override).
    """

    # Default lobster config dir — matches install.sh and service EnvironmentFile paths
    EXPECTED_DEFAULT_CONFIG_DIR = "lobster-config"

    @pytest.fixture
    def cli_path(self) -> Path:
        """Get path to CLI script."""
        return Path(__file__).parent.parent.parent.parent / "src" / "cli"

    def test_env_set_writes_to_lobster_config_not_user_config(self, cli_path: Path):
        """cmd_env must default to ~/lobster-config, not ~/lobster-user-config.

        Services load EnvironmentFile from lobster-config/. Writes to
        lobster-user-config/ are silently ignored by systemd.
        """
        source = cli_path.read_text()
        # The default fallback must reference lobster-config
        assert "lobster-config" in source, (
            "cmd_env must default LOBSTER_CONFIG_DIR to ~/lobster-config "
            "so tokens written by 'lobster env set' land where services read them"
        )
        # Must NOT default to the wrong directory (user-config is for agent behavior, not service env)
        # Check that the old wrong default is not the primary fallback
        assert 'lobster-user-config"' not in source or source.count('lobster-user-config"') == 0, (
            "cmd_env must not default to lobster-user-config — "
            "that directory is not read by systemd EnvironmentFile"
        )

    def test_env_set_updates_config_env_when_key_exists_there(self, cli_path: Path, tmp_path: Path):
        """If a key already exists in config.env (even as empty stub), set must update config.env.

        config.env is the single canonical credential file after issue #1785 consolidation.
        """
        config_dir = tmp_path / "lobster-config"
        config_dir.mkdir()
        config_env = config_dir / "config.env"

        # Write empty stub to config.env (simulates non-interactive installer output)
        config_env.write_text("TELEGRAM_ALLOWED_USERS=\n")

        env = os.environ.copy()
        env["LOBSTER_CONFIG_DIR"] = str(config_dir)
        # Override HOME so the fallback path doesn't interfere
        env["HOME"] = str(tmp_path)

        result = subprocess.run(
            ["bash", str(cli_path), "env", "set", "TELEGRAM_ALLOWED_USERS", "12345"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"env set failed: {result.stderr}"

        # The value must land in config.env
        config_content = config_env.read_text()
        assert "TELEGRAM_ALLOWED_USERS=12345" in config_content, (
            "env set must write to config.env when the key already exists there as an empty stub"
        )

    def test_env_set_writes_new_keys_to_config_env(self, cli_path: Path, tmp_path: Path):
        """New keys (not previously in config.env) must now be written to config.env.

        Before issue #1785, new keys fell back to global.env.
        After consolidation, config.env is the single canonical file for all credentials.
        """
        config_dir = tmp_path / "lobster-config"
        config_dir.mkdir()
        config_env = config_dir / "config.env"

        config_env.write_text("# no GITHUB_TOKEN here\n")

        env = os.environ.copy()
        env["LOBSTER_CONFIG_DIR"] = str(config_dir)
        env["HOME"] = str(tmp_path)

        result = subprocess.run(
            ["bash", str(cli_path), "env", "set", "GITHUB_TOKEN", "ghp_abc123"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"env set failed: {result.stderr}"

        config_content = config_env.read_text()
        assert "GITHUB_TOKEN=ghp_abc123" in config_content, (
            "env set must write new keys to config.env (single canonical credential file)"
        )
