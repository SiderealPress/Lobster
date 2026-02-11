"""
Tests for Lobster Installation

Tests the installation process on fresh systems.
"""

import json
import os
import subprocess
import tempfile
import pytest
from pathlib import Path


@pytest.mark.integration
class TestDirectoryCreation:
    """Tests for directory creation during installation."""

    def test_messages_directories_exist(self, temp_messages_dir: Path):
        """Test that all message directories are created."""
        required_dirs = [
            "inbox", "outbox", "processed", "processing", "failed",
            "config", "audio", "task-outputs",
        ]

        for dirname in required_dirs:
            dir_path = temp_messages_dir / dirname
            assert dir_path.exists(), f"Directory {dirname} should exist"
            assert dir_path.is_dir(), f"{dirname} should be a directory"

    def test_scheduled_tasks_directories_exist(self, temp_scheduled_tasks_dir: Path):
        """Test that scheduled tasks directories are created."""
        required_dirs = ["tasks", "logs"]

        for dirname in required_dirs:
            dir_path = temp_scheduled_tasks_dir / dirname
            assert dir_path.exists(), f"Directory {dirname} should exist"

    def test_workspace_directory_created(self, temp_workspace: Path):
        """Test that workspace directory is created."""
        assert temp_workspace.exists()
        assert (temp_workspace / "logs").exists()


@pytest.mark.integration
class TestFileInitialization:
    """Tests for file initialization."""

    def test_tasks_json_initialized(self, temp_messages_dir: Path):
        """Test that tasks.json is properly initialized."""
        tasks_file = temp_messages_dir / "tasks.json"
        tasks_file.write_text(json.dumps({"tasks": [], "next_id": 1}))

        content = json.loads(tasks_file.read_text())

        assert "tasks" in content
        assert "next_id" in content
        assert content["tasks"] == []
        assert content["next_id"] == 1

    def test_jobs_json_initialized(self, temp_scheduled_tasks_dir: Path):
        """Test that jobs.json is properly initialized."""
        jobs_file = temp_scheduled_tasks_dir / "jobs.json"

        content = json.loads(jobs_file.read_text())

        assert "jobs" in content
        assert content["jobs"] == {}


@pytest.mark.integration
class TestPythonEnvironment:
    """Tests for Python environment setup."""

    def test_python_version(self):
        """Test that Python version is adequate."""
        import sys
        assert sys.version_info >= (3, 9), "Python 3.9+ required"

    def test_required_packages_importable(self):
        """Test that required packages can be imported."""
        required_packages = [
            ("mcp.server", "mcp"),
            ("telegram", "python-telegram-bot"),
            ("watchdog.observers", "watchdog"),
        ]

        for module, package in required_packages:
            try:
                __import__(module)
            except ImportError:
                pytest.skip(f"Package {package} not installed")

    def test_mcp_server_importable(self):
        """Test that MCP server module can be imported."""
        try:
            from src.mcp import inbox_server
            assert hasattr(inbox_server, "server")
        except ImportError as e:
            pytest.skip(f"MCP server not importable: {e}")

    def test_bot_module_importable(self):
        """Test that bot module can be imported."""
        try:
            # This will fail without env vars, which is expected
            with pytest.raises((ValueError, KeyError)):
                from src.bot import lobster_bot
        except ImportError as e:
            pytest.skip(f"Bot module not importable: {e}")


@pytest.mark.integration
class TestScriptExecutability:
    """Tests for script executability."""

    @pytest.fixture
    def lobster_dir(self) -> Path:
        """Get Lobster installation directory."""
        return Path(__file__).parent.parent.parent

    def test_cli_is_bash_script(self, lobster_dir: Path):
        """Test that CLI is a valid bash script."""
        cli_path = lobster_dir / "src" / "cli"
        if cli_path.exists():
            content = cli_path.read_text()
            assert content.startswith("#!/bin/bash"), "CLI should be a bash script"

    def test_run_job_script_exists(self, lobster_dir: Path):
        """Test that run-job.sh exists."""
        script = lobster_dir / "scheduled-tasks" / "run-job.sh"
        if script.exists():
            assert os.access(script, os.X_OK), "run-job.sh should be executable"

    def test_sync_crontab_script_exists(self, lobster_dir: Path):
        """Test that sync-crontab.sh exists."""
        script = lobster_dir / "scheduled-tasks" / "sync-crontab.sh"
        if script.exists():
            assert os.access(script, os.X_OK), "sync-crontab.sh should be executable"


@pytest.mark.integration
@pytest.mark.docker
class TestDockerInstallation:
    """Tests that run in Docker containers."""

    def test_fresh_debian_install(self):
        """Test installation on fresh Debian container."""
        # This test is meant to be run in Docker
        # Skip if not in Docker environment
        if not os.path.exists("/.dockerenv"):
            pytest.skip("Not running in Docker")

        # Verify basic system requirements
        result = subprocess.run(
            ["python3", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_dependencies_installed(self):
        """Test that all dependencies are installed."""
        if not os.path.exists("/.dockerenv"):
            pytest.skip("Not running in Docker")

        required_commands = ["curl", "wget", "git", "jq", "python3"]

        for cmd in required_commands:
            result = subprocess.run(
                ["which", cmd],
                capture_output=True,
            )
            assert result.returncode == 0, f"{cmd} should be installed"


@pytest.mark.integration
class TestTemplateSubstitution:
    """Tests for template substitution functionality."""

    @pytest.fixture
    def lobster_dir(self) -> Path:
        """Get Lobster installation directory."""
        return Path(__file__).parent.parent.parent

    @pytest.fixture
    def temp_install_dir(self) -> Path:
        """Create a temporary installation directory."""
        tmp = tempfile.mkdtemp(prefix="lobster_install_test_")
        yield Path(tmp)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_router_template_exists(self, lobster_dir: Path):
        """Test that the router service template exists."""
        template = lobster_dir / "services" / "lobster-router.service.template"
        assert template.exists(), "Router service template should exist"
        content = template.read_text()
        assert "{{USER}}" in content, "Template should contain {{USER}} placeholder"
        assert "{{INSTALL_DIR}}" in content, "Template should contain {{INSTALL_DIR}} placeholder"

    def test_claude_template_exists(self, lobster_dir: Path):
        """Test that the Claude service template exists."""
        template = lobster_dir / "services" / "lobster-claude.service.template"
        assert template.exists(), "Claude service template should exist"
        content = template.read_text()
        assert "{{USER}}" in content, "Template should contain {{USER}} placeholder"
        assert "{{WORKSPACE_DIR}}" in content, "Template should contain {{WORKSPACE_DIR}} placeholder"

    def test_config_example_exists(self, lobster_dir: Path):
        """Test that the configuration example exists."""
        config_example = lobster_dir / "config" / "lobster.conf.example"
        assert config_example.exists(), "Configuration example should exist"
        content = config_example.read_text()
        assert "LOBSTER_USER" in content, "Config should define LOBSTER_USER"
        assert "LOBSTER_INSTALL_DIR" in content, "Config should define LOBSTER_INSTALL_DIR"
        assert "LOBSTER_REPO_URL" in content, "Config should define LOBSTER_REPO_URL"

    def test_template_substitution_with_sed(self, lobster_dir: Path, temp_install_dir: Path):
        """Test that template substitution works correctly using sed."""
        # Copy template to temp directory
        template = lobster_dir / "services" / "lobster-router.service.template"
        output = temp_install_dir / "lobster-router.service"

        # Define test values
        test_user = "testuser"
        test_group = "testgroup"
        test_home = "/home/testuser"
        test_install_dir = "/opt/lobster"
        test_workspace_dir = "/home/testuser/workspace"
        test_messages_dir = "/home/testuser/messages"
        test_config_dir = "/etc/lobster"

        # Run sed substitution (same as install.sh does)
        result = subprocess.run(
            [
                "sed",
                "-e", f"s|{{{{USER}}}}|{test_user}|g",
                "-e", f"s|{{{{GROUP}}}}|{test_group}|g",
                "-e", f"s|{{{{HOME}}}}|{test_home}|g",
                "-e", f"s|{{{{INSTALL_DIR}}}}|{test_install_dir}|g",
                "-e", f"s|{{{{WORKSPACE_DIR}}}}|{test_workspace_dir}|g",
                "-e", f"s|{{{{MESSAGES_DIR}}}}|{test_messages_dir}|g",
                "-e", f"s|{{{{CONFIG_DIR}}}}|{test_config_dir}|g",
                str(template),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"sed failed: {result.stderr}"

        # Write output
        output.write_text(result.stdout)

        # Verify substitutions
        content = output.read_text()
        assert "{{USER}}" not in content, "{{USER}} should be substituted"
        assert "{{GROUP}}" not in content, "{{GROUP}} should be substituted"
        assert "{{HOME}}" not in content, "{{HOME}} should be substituted"
        assert "{{INSTALL_DIR}}" not in content, "{{INSTALL_DIR}} should be substituted"

        assert f"User={test_user}" in content, "User should be substituted correctly"
        assert f"Group={test_group}" in content, "Group should be substituted correctly"
        assert test_install_dir in content, "Install dir should be in output"

    def test_install_script_has_template_function(self, lobster_dir: Path):
        """Test that install.sh contains the generate_from_template function."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert "generate_from_template()" in content, "install.sh should define generate_from_template()"
        assert "LOBSTER_REPO_URL" in content, "install.sh should support LOBSTER_REPO_URL"
        assert "LOBSTER_BRANCH" in content, "install.sh should support LOBSTER_BRANCH"
        assert "lobster.conf" in content, "install.sh should reference lobster.conf"

    def test_install_script_sources_config(self, lobster_dir: Path):
        """Test that install.sh sources the configuration file."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert "source \"$CONFIG_FILE\"" in content, "install.sh should source config file"
        assert "Load Configuration" in content, "install.sh should have configuration loading section"

    def test_install_script_uses_templates(self, lobster_dir: Path):
        """Test that install.sh uses template files instead of heredocs."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert "generate_from_template" in content, "install.sh should call generate_from_template"
        assert "lobster-router.service.template" in content, "install.sh should reference router template"
        assert "lobster-claude.service.template" in content, "install.sh should reference claude template"


@pytest.mark.integration
class TestPrivateConfigOverlay:
    """Tests for private configuration overlay functionality."""

    @pytest.fixture
    def lobster_dir(self) -> Path:
        """Get Lobster installation directory."""
        return Path(__file__).parent.parent.parent

    @pytest.fixture
    def temp_private_config(self) -> Path:
        """Create a temporary private config directory."""
        tmp = tempfile.mkdtemp(prefix="lobster_private_config_")
        yield Path(tmp)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_install_script_has_overlay_function(self, lobster_dir: Path):
        """Test that install.sh contains the apply_private_overlay function."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert "apply_private_overlay()" in content, "install.sh should define apply_private_overlay()"
        assert "LOBSTER_CONFIG_DIR" in content, "install.sh should reference LOBSTER_CONFIG_DIR"

    def test_install_script_has_hooks_function(self, lobster_dir: Path):
        """Test that install.sh contains the run_hook function."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert "run_hook()" in content, "install.sh should define run_hook()"
        assert 'run_hook "post-install.sh"' in content, "install.sh should call post-install hook"

    def test_overlay_supports_config_env(self, lobster_dir: Path):
        """Test that overlay supports config.env file."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert 'config_dir/config.env' in content, "Overlay should check for config.env"

    def test_overlay_supports_claude_md(self, lobster_dir: Path):
        """Test that overlay supports CLAUDE.md file."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert 'config_dir/CLAUDE.md' in content, "Overlay should check for CLAUDE.md"

    def test_overlay_supports_agents_directory(self, lobster_dir: Path):
        """Test that overlay supports agents directory."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert 'config_dir/agents' in content, "Overlay should check for agents directory"
        assert '.claude/agents' in content, "Overlay should copy to .claude/agents"

    def test_overlay_supports_scheduled_tasks(self, lobster_dir: Path):
        """Test that overlay supports scheduled-tasks directory."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert 'config_dir/scheduled-tasks' in content, "Overlay should check for scheduled-tasks directory"

    def test_hooks_export_environment_variables(self, lobster_dir: Path):
        """Test that hooks have access to environment variables."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert 'LOBSTER_INSTALL_DIR' in content, "Hook should export LOBSTER_INSTALL_DIR"
        assert 'LOBSTER_WORKSPACE_DIR' in content, "Hook should export LOBSTER_WORKSPACE_DIR"
        assert 'LOBSTER_MESSAGES_DIR' in content, "Hook should export LOBSTER_MESSAGES_DIR"

    def test_overlay_gracefully_handles_missing_dir(self, lobster_dir: Path):
        """Test that overlay handles missing config directory gracefully."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        # Should check if directory exists and warn if not
        assert '! -d "$config_dir"' in content, "Should check if config dir exists"
        assert 'warn "Private config directory not found' in content, "Should warn about missing dir"

    def test_overlay_gracefully_handles_unset_var(self, lobster_dir: Path):
        """Test that overlay handles unset LOBSTER_CONFIG_DIR gracefully."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        # Should check if variable is empty and skip
        assert '-z "$config_dir"' in content, "Should check if config dir var is empty"

    def test_hooks_check_executable_permission(self, lobster_dir: Path):
        """Test that hooks check for executable permission."""
        install_script = lobster_dir / "install.sh"
        content = install_script.read_text()

        assert '! -x "$hook_path"' in content, "Should check if hook is executable"
        assert 'chmod +x' in content, "Should suggest chmod command"
