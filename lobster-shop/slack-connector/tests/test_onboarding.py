"""Tests for slack-connector onboarding module.

Tests pure validation functions directly and uses mocks for API/IO boundaries.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import path setup — the skill src is not a proper installed package
import sys
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent),
)

from src.onboarding import (
    BOT_TOKEN_PREFIX,
    APP_TOKEN_PREFIX,
    PrerequisiteResult,
    SlackOnboarding,
    TokenPair,
    ValidationResult,
    build_updated_config,
    check_config_env_writable,
    check_python_version,
    failed_prerequisites,
    mask_token,
    read_config_tokens,
    validate_app_token_format,
    validate_bot_token_format,
    validate_bot_token_with_api,
    write_tokens_to_config,
)


# ============================================================================
# Pure validation: bot token format
# ============================================================================

class TestValidateBotTokenFormat:
    def test_valid_token(self):
        result = validate_bot_token_format("xoxb-123-456-abc")
        assert result.valid is True
        assert "valid" in result.message.lower()

    def test_empty_token(self):
        result = validate_bot_token_format("")
        assert result.valid is False
        assert "empty" in result.message.lower()

    def test_whitespace_only(self):
        result = validate_bot_token_format("   ")
        assert result.valid is False

    def test_wrong_prefix(self):
        result = validate_bot_token_format("xoxp-123-456-abc")
        assert result.valid is False
        assert "xoxb-" in result.message

    def test_too_few_parts(self):
        result = validate_bot_token_format("xoxb-123")
        assert result.valid is False
        assert "format" in result.message.lower()

    def test_strips_whitespace(self):
        result = validate_bot_token_format("  xoxb-123-456-abc  ")
        assert result.valid is True

    def test_real_format_token(self):
        # Realistic token structure — constructed dynamically to avoid
        # triggering GitHub push protection on literal xoxb- strings
        token = BOT_TOKEN_PREFIX + "000000000000-000000000000-FAKEFAKEFAKE"
        result = validate_bot_token_format(token)
        assert result.valid is True


# ============================================================================
# Pure validation: app token format
# ============================================================================

class TestValidateAppTokenFormat:
    def test_valid_token(self):
        result = validate_app_token_format("xapp-1-A0APJ2RRW5S-108484-secret")
        assert result.valid is True

    def test_empty_token(self):
        result = validate_app_token_format("")
        assert result.valid is False

    def test_wrong_prefix(self):
        result = validate_app_token_format("xoxb-wrong-type")
        assert result.valid is False
        assert "xapp-" in result.message

    def test_strips_whitespace(self):
        result = validate_app_token_format("  xapp-1-valid  ")
        assert result.valid is True


# ============================================================================
# Pure: prerequisite checks
# ============================================================================

class TestCheckPythonVersion:
    def test_meets_requirement(self):
        result = check_python_version(3, 11, current=(3, 12))
        assert result.passed is True
        assert "3.12" in result.message

    def test_exact_match(self):
        result = check_python_version(3, 11, current=(3, 11))
        assert result.passed is True

    def test_below_requirement(self):
        result = check_python_version(3, 11, current=(3, 10))
        assert result.passed is False
        assert "required" in result.message.lower()

    def test_major_too_low(self):
        result = check_python_version(3, 11, current=(2, 7))
        assert result.passed is False


class TestCheckConfigEnvWritable:
    def test_writable_file(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("KEY=value\n")
        result = check_config_env_writable(config)
        assert result.passed is True

    def test_missing_file(self, tmp_path):
        result = check_config_env_writable(tmp_path / "nope.env")
        assert result.passed is False
        assert "does not exist" in result.message

    def test_not_writable(self, tmp_path):
        config = tmp_path / "readonly.env"
        config.write_text("KEY=value\n")
        config.chmod(0o444)
        result = check_config_env_writable(config)
        # Root can write to readonly files, so skip assertion if running as root
        if os.getuid() != 0:
            assert result.passed is False


class TestFailedPrerequisites:
    def test_filters_failures(self):
        results = [
            PrerequisiteResult("a", True, "ok"),
            PrerequisiteResult("b", False, "bad"),
            PrerequisiteResult("c", True, "ok"),
            PrerequisiteResult("d", False, "worse"),
        ]
        failures = failed_prerequisites(results)
        assert len(failures) == 2
        assert all(not r.passed for r in failures)

    def test_all_passing(self):
        results = [PrerequisiteResult("a", True, "ok")]
        assert failed_prerequisites(results) == []

    def test_empty_input(self):
        assert failed_prerequisites([]) == []


# ============================================================================
# Pure: mask_token
# ============================================================================

class TestMaskToken:
    def test_long_token(self):
        masked = mask_token("xoxb-123456789-abcdefgh")
        assert masked.startswith("xoxb-")
        assert masked.endswith("efgh")
        assert "****" in masked

    def test_short_token(self):
        masked = mask_token("xoxb-tiny")
        assert masked.startswith("xoxb-")
        assert "****" in masked

    def test_exact_boundary(self):
        masked = mask_token("xoxb-12345")  # len=10
        assert "****" in masked


# ============================================================================
# Pure: build_updated_config
# ============================================================================

class TestBuildUpdatedConfig:
    def test_replaces_existing_tokens(self):
        existing = textwrap.dedent("""\
            # Config
            LOBSTER_SLACK_BOT_TOKEN=old-bot-token
            LOBSTER_SLACK_APP_TOKEN=old-app-token
            OTHER_KEY=value
        """)
        result = build_updated_config(existing, "xoxb-new", "xapp-new")
        assert "LOBSTER_SLACK_BOT_TOKEN=xoxb-new" in result
        assert "LOBSTER_SLACK_APP_TOKEN=xapp-new" in result
        assert "old-bot-token" not in result
        assert "old-app-token" not in result
        assert "OTHER_KEY=value" in result

    def test_appends_missing_tokens(self):
        existing = "SOME_KEY=value\n"
        result = build_updated_config(existing, "xoxb-new", "xapp-new")
        assert "LOBSTER_SLACK_BOT_TOKEN=xoxb-new" in result
        assert "LOBSTER_SLACK_APP_TOKEN=xapp-new" in result
        assert "# Slack Integration" in result
        assert "SOME_KEY=value" in result

    def test_no_duplication_on_rerun(self):
        existing = "LOBSTER_SLACK_BOT_TOKEN=xoxb-1\nLOBSTER_SLACK_APP_TOKEN=xapp-1\n"
        result = build_updated_config(existing, "xoxb-1", "xapp-1")
        assert result.count("LOBSTER_SLACK_BOT_TOKEN") == 1
        assert result.count("LOBSTER_SLACK_APP_TOKEN") == 1

    def test_mixed_existing_and_missing(self):
        existing = "LOBSTER_SLACK_BOT_TOKEN=xoxb-existing\n"
        result = build_updated_config(existing, "xoxb-existing", "xapp-new")
        assert result.count("LOBSTER_SLACK_BOT_TOKEN") == 1
        assert "LOBSTER_SLACK_APP_TOKEN=xapp-new" in result

    def test_empty_file(self):
        result = build_updated_config("", "xoxb-new", "xapp-new")
        assert "LOBSTER_SLACK_BOT_TOKEN=xoxb-new" in result
        assert "LOBSTER_SLACK_APP_TOKEN=xapp-new" in result

    def test_trailing_newline(self):
        result = build_updated_config("KEY=val", "xoxb-1", "xapp-1")
        assert result.endswith("\n")

    def test_preserves_other_content(self):
        existing = textwrap.dedent("""\
            # Telegram
            TELEGRAM_BOT_TOKEN=xxx

            # GitHub
            GITHUB_TOKEN=yyy
        """)
        result = build_updated_config(existing, "xoxb-1", "xapp-1")
        assert "TELEGRAM_BOT_TOKEN=xxx" in result
        assert "GITHUB_TOKEN=yyy" in result
        assert "# Telegram" in result


# ============================================================================
# Side-effectful: read_config_tokens
# ============================================================================

class TestReadConfigTokens:
    def test_reads_both_tokens(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text(
            "LOBSTER_SLACK_BOT_TOKEN=xoxb-bot\n"
            "LOBSTER_SLACK_APP_TOKEN=xapp-app\n"
        )
        tokens = read_config_tokens(config)
        assert tokens["bot_token"] == "xoxb-bot"
        assert tokens["app_token"] == "xapp-app"

    def test_missing_file(self, tmp_path):
        tokens = read_config_tokens(tmp_path / "nope.env")
        assert tokens["bot_token"] == ""
        assert tokens["app_token"] == ""

    def test_partial_config(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("LOBSTER_SLACK_BOT_TOKEN=xoxb-only\n")
        tokens = read_config_tokens(config)
        assert tokens["bot_token"] == "xoxb-only"
        assert tokens["app_token"] == ""

    def test_strips_quotes(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text(
            'LOBSTER_SLACK_BOT_TOKEN="xoxb-quoted"\n'
            "LOBSTER_SLACK_APP_TOKEN='xapp-single'\n"
        )
        tokens = read_config_tokens(config)
        assert tokens["bot_token"] == "xoxb-quoted"
        assert tokens["app_token"] == "xapp-single"


# ============================================================================
# Side-effectful: write_tokens_to_config
# ============================================================================

class TestWriteTokensToConfig:
    def test_write_to_existing(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("EXISTING=value\n")
        write_tokens_to_config(config, "xoxb-bot", "xapp-app")
        content = config.read_text()
        assert "LOBSTER_SLACK_BOT_TOKEN=xoxb-bot" in content
        assert "LOBSTER_SLACK_APP_TOKEN=xapp-app" in content
        assert "EXISTING=value" in content

    def test_idempotent(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("KEY=val\n")
        write_tokens_to_config(config, "xoxb-1", "xapp-1")
        first = config.read_text()
        write_tokens_to_config(config, "xoxb-1", "xapp-1")
        second = config.read_text()
        assert first == second

    def test_updates_existing_tokens(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text(
            "LOBSTER_SLACK_BOT_TOKEN=old\n"
            "LOBSTER_SLACK_APP_TOKEN=old\n"
        )
        write_tokens_to_config(config, "xoxb-new", "xapp-new")
        content = config.read_text()
        assert content.count("LOBSTER_SLACK_BOT_TOKEN") == 1
        assert "LOBSTER_SLACK_BOT_TOKEN=xoxb-new" in content


# ============================================================================
# API validation (mocked)
# ============================================================================

class TestValidateBotTokenWithApi:
    def _make_mock_slack_sdk(self, auth_response):
        """Build a fake slack_sdk module whose WebClient returns auth_response."""
        mock_client_instance = MagicMock()
        mock_client_instance.auth_test.return_value = auth_response

        mock_webclient_cls = MagicMock(return_value=mock_client_instance)

        mock_sdk = MagicMock()
        mock_sdk.WebClient = mock_webclient_cls

        mock_errors = MagicMock()
        mock_errors.SlackApiError = type("SlackApiError", (Exception,), {})

        return {"slack_sdk": mock_sdk, "slack_sdk.errors": mock_errors}

    def test_valid_token(self):
        """Test auth.test success path."""
        modules = self._make_mock_slack_sdk({
            "ok": True, "team": "Acme Corp", "user": "lobster",
        })
        with patch.dict("sys.modules", modules):
            ok, msg = validate_bot_token_with_api("xoxb-test-123-456")
        assert ok is True
        assert "Acme Corp" in msg

    def test_failed_auth_test(self):
        """Test auth.test failure path."""
        modules = self._make_mock_slack_sdk({
            "ok": False, "error": "invalid_auth",
        })
        with patch.dict("sys.modules", modules):
            ok, msg = validate_bot_token_with_api("xoxb-bad-token-here")
        assert ok is False
        assert "invalid_auth" in msg

    def test_import_error(self):
        """Test graceful handling when slack-sdk not installed."""
        # Setting a module to None in sys.modules causes ImportError on import
        with patch.dict("sys.modules", {"slack_sdk": None}):
            ok, msg = validate_bot_token_with_api("xoxb-123-456-abc")
        assert ok is False
        assert "not installed" in msg.lower()


# ============================================================================
# SlackOnboarding orchestrator
# ============================================================================

class TestSlackOnboardingPrerequisites:
    def test_all_pass(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("KEY=value\n")
        onboarding = SlackOnboarding(config_path=config)
        with patch("src.onboarding.check_slack_bolt_installed") as mock_bolt:
            mock_bolt.return_value = PrerequisiteResult("slack-bolt", True, "installed")
            failures = onboarding.check_prerequisites()
        assert failures == []

    def test_reports_failures(self, tmp_path):
        # Missing config file
        onboarding = SlackOnboarding(config_path=tmp_path / "missing.env")
        failures = onboarding.check_prerequisites()
        assert any("does not exist" in f for f in failures)


class TestSlackOnboardingWizard:
    def test_skips_when_tokens_exist(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text(
            "LOBSTER_SLACK_BOT_TOKEN=xoxb-existing-123-456\n"
            "LOBSTER_SLACK_APP_TOKEN=xapp-existing-secret\n"
        )
        outputs = []
        onboarding = SlackOnboarding(
            config_path=config,
            print_fn=outputs.append,
            input_fn=lambda _: "",
        )
        with patch("src.onboarding.check_slack_bolt_installed") as mock_bolt:
            mock_bolt.return_value = PrerequisiteResult("slack-bolt", True, "ok")
            result = onboarding.run_setup_wizard()

        assert result is True
        assert any("already configured" in str(o) for o in outputs)

    def test_fails_on_bad_prerequisites(self, tmp_path):
        outputs = []
        onboarding = SlackOnboarding(
            config_path=tmp_path / "missing.env",
            print_fn=outputs.append,
            input_fn=lambda _: "",
        )
        result = onboarding.run_setup_wizard()
        assert result is False
        assert any("failed" in str(o).lower() for o in outputs)

    def test_collects_and_writes_tokens(self, tmp_path):
        config = tmp_path / "config.env"
        config.write_text("OTHER=value\n")

        # Simulate user input sequence
        inputs = iter([
            "",  # Press Enter after guide
            "xoxb-123-456-abc",  # Bot token
            "xapp-1-secret",  # App token
        ])
        outputs = []

        onboarding = SlackOnboarding(
            config_path=config,
            print_fn=outputs.append,
            input_fn=lambda _: next(inputs),
        )

        with patch("src.onboarding.check_slack_bolt_installed") as mock_bolt, \
             patch("src.onboarding.validate_bot_token_with_api") as mock_api:
            mock_bolt.return_value = PrerequisiteResult("slack-bolt", True, "ok")
            mock_api.return_value = (True, "TestWorkspace")

            result = onboarding.run_setup_wizard()

        assert result is True
        content = config.read_text()
        assert "LOBSTER_SLACK_BOT_TOKEN=xoxb-123-456-abc" in content
        assert "LOBSTER_SLACK_APP_TOKEN=xapp-1-secret" in content
        assert "OTHER=value" in content
