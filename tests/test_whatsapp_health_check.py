"""
Tests for the WhatsApp health check integration — BIS-50.

Tests:
- Service file structure (required systemd fields present)
- Health check script validates correctly
- Config example has required fields
- Logrotate config is parseable
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


class TestServiceFile:
    """Validate the systemd service file for lobster-whatsapp-bridge."""

    SERVICE_FILE = REPO_ROOT / "services" / "lobster-whatsapp-bridge.service"

    def test_service_file_exists(self):
        assert self.SERVICE_FILE.exists(), f"Service file not found: {self.SERVICE_FILE}"

    def test_service_file_has_unit_section(self):
        content = self.SERVICE_FILE.read_text()
        assert "[Unit]" in content

    def test_service_file_has_service_section(self):
        content = self.SERVICE_FILE.read_text()
        assert "[Service]" in content

    def test_service_file_has_install_section(self):
        content = self.SERVICE_FILE.read_text()
        assert "[Install]" in content

    def test_service_file_has_description(self):
        content = self.SERVICE_FILE.read_text()
        assert "Description=" in content
        assert "WhatsApp" in content

    def test_service_file_has_execstart(self):
        content = self.SERVICE_FILE.read_text()
        assert "ExecStart=" in content
        assert "node" in content.lower()

    def test_service_file_has_restart_policy(self):
        content = self.SERVICE_FILE.read_text()
        assert "Restart=always" in content

    def test_service_file_has_user(self):
        content = self.SERVICE_FILE.read_text()
        assert "User=" in content

    def test_service_file_has_log_redirect(self):
        content = self.SERVICE_FILE.read_text()
        assert "whatsapp-bridge.log" in content

    def test_service_file_has_env_file(self):
        content = self.SERVICE_FILE.read_text()
        assert "EnvironmentFile" in content
        assert "whatsapp.env" in content

    def test_service_file_no_pii(self):
        """Ensure no phone numbers or personal data in the service file."""
        content = self.SERVICE_FILE.read_text()
        # Basic PII checks: no E.164 phone numbers embedded
        import re
        # Match patterns like +1-555-123-4567 or 15551234567 (10+ digit runs)
        phone_pattern = re.compile(r'\+?1?\s*[\-.]?\s*\(?\d{3}\)?\s*[\-.]?\s*\d{3}\s*[\-.]?\s*\d{4}')
        matches = phone_pattern.findall(content)
        # Allow version numbers and port numbers (short digit sequences in context)
        for match in matches:
            digits_only = re.sub(r'\D', '', match)
            if len(digits_only) >= 10:
                pytest.fail(f"Possible phone number in service file: {match!r}")


class TestAdapterServiceFile:
    """Validate the lobster-whatsapp-adapter.service file."""

    SERVICE_FILE = REPO_ROOT / "services" / "lobster-whatsapp-adapter.service"

    def test_adapter_service_file_exists(self):
        assert self.SERVICE_FILE.exists(), f"Adapter service file not found: {self.SERVICE_FILE}"

    def test_adapter_service_has_python_execstart(self):
        content = self.SERVICE_FILE.read_text()
        assert "ExecStart=" in content
        assert "python" in content.lower() or "whatsapp_bridge_adapter" in content

    def test_adapter_requires_bridge(self):
        content = self.SERVICE_FILE.read_text()
        # Adapter should depend on the bridge
        assert "lobster-whatsapp-bridge" in content


class TestConfigExample:
    """Validate the whatsapp.env.example config file."""

    CONFIG_FILE = REPO_ROOT / "config" / "whatsapp.env.example"

    def test_config_example_exists(self):
        assert self.CONFIG_FILE.exists()

    def test_config_has_lobster_jid_key(self):
        content = self.CONFIG_FILE.read_text()
        assert "WHATSAPP_LOBSTER_JID" in content

    def test_config_jid_value_is_empty(self):
        """The example file should not have a real JID filled in."""
        content = self.CONFIG_FILE.read_text()
        for line in content.splitlines():
            if line.startswith("WHATSAPP_LOBSTER_JID="):
                value = line.split("=", 1)[1].strip()
                assert value == "", f"WHATSAPP_LOBSTER_JID should be empty in example, got: {value!r}"
                break

    def test_config_no_real_phone_numbers(self):
        """Example file must not contain real phone numbers (only placeholder examples like 15551234567)."""
        import re
        content = self.CONFIG_FILE.read_text()
        # Look for JID patterns with non-placeholder content
        jid_pattern = re.compile(r'\d{10,}@[cs]\.us')
        matches = jid_pattern.findall(content)
        # Well-known placeholder/example numbers are allowed:
        # - Numbers with 4+ repeated digits (e.g. 0000, 1111, 5555)
        # - NANP fictional numbers: 555-01xx through 555-0199 range often used in docs
        # - 15551234567 is a standard placeholder (555 exchange is reserved for fiction)
        KNOWN_PLACEHOLDERS = {"15551234567@c.us", "19995551234@c.us"}
        real_matches = [
            m for m in matches
            if m not in KNOWN_PLACEHOLDERS
            and not any(c * 4 in re.sub(r'\D', '', m.split('@')[0]) for c in '0123456789')
        ]
        if real_matches:
            pytest.fail(f"Possible real phone number in config example: {real_matches}")


class TestHealthCheckScript:
    """Validate the whatsapp health check shell script."""

    SCRIPT = REPO_ROOT / "scripts" / "whatsapp-health-check.sh"

    def test_script_exists(self):
        assert self.SCRIPT.exists()

    def test_script_is_executable(self):
        assert os.access(self.SCRIPT, os.X_OK), f"{self.SCRIPT} is not executable"

    def test_script_has_shebang(self):
        content = self.SCRIPT.read_text()
        assert content.startswith("#!/bin/bash") or content.startswith("#!/usr/bin/env bash")

    def test_script_checks_service_status(self):
        content = self.SCRIPT.read_text()
        assert "lobster-whatsapp-bridge" in content
        assert "systemctl" in content

    def test_script_writes_to_inbox(self):
        content = self.SCRIPT.read_text()
        assert "inbox" in content

    def test_script_checks_heartbeat(self):
        content = self.SCRIPT.read_text()
        assert "heartbeat" in content.lower() or "HEARTBEAT" in content

    def test_script_bash_syntax(self):
        """Check the script has valid bash syntax (if bash is available)."""
        result = subprocess.run(
            ["bash", "-n", str(self.SCRIPT)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"Bash syntax error in {self.SCRIPT}:\n{result.stderr}")


class TestLogrotateConfig:
    """Validate the logrotate configuration file."""

    LOGROTATE_FILE = REPO_ROOT / "logrotate" / "lobster-whatsapp"

    def test_logrotate_file_exists(self):
        assert self.LOGROTATE_FILE.exists()

    def test_logrotate_references_bridge_log(self):
        content = self.LOGROTATE_FILE.read_text()
        assert "whatsapp-bridge.log" in content

    def test_logrotate_has_rotation_settings(self):
        content = self.LOGROTATE_FILE.read_text()
        assert "daily" in content or "weekly" in content
        assert "rotate" in content
        assert "compress" in content

    def test_logrotate_has_missingok(self):
        content = self.LOGROTATE_FILE.read_text()
        assert "missingok" in content


class TestInstallScript:
    """Validate the install script structure."""

    INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-whatsapp-connector.sh"

    def test_install_script_exists(self):
        assert self.INSTALL_SCRIPT.exists()

    def test_install_script_is_executable(self):
        assert os.access(self.INSTALL_SCRIPT, os.X_OK)

    def test_install_script_mentions_npm_install(self):
        content = self.INSTALL_SCRIPT.read_text()
        assert "npm install" in content

    def test_install_script_mentions_systemctl(self):
        content = self.INSTALL_SCRIPT.read_text()
        assert "systemctl" in content

    def test_install_script_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(self.INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"Bash syntax error in {self.INSTALL_SCRIPT}:\n{result.stderr}")
