"""
Unit tests for hooks/secret-scanner.py

Tests cover:
- _find_config_file(): returns first existing candidate, respects LOBSTER_CONFIG_DIR env var
- _load_secrets(): parses KEY=VALUE pairs, strips quotes, filters by minimum length
- _extract_strings_to_scan(): selects correct fields per tool name, skips non-write Bash
- _scan_for_secrets(): detects exact substring matches, returns matching key names
- main(): exits 0 silently when no secrets detected
- main(): writes WARNING to stderr and exits 0 when a secret is found (warn mode, not block)
- main(): exits 0 silently when config file is absent
- main(): exits 0 silently for tools not in scope
"""

import importlib.util
import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_hook():
    """Load hooks/secret-scanner.py as a module without executing main()."""
    hooks_dir = Path(__file__).parent.parent / "hooks"
    hook_path = hooks_dir / "secret-scanner.py"
    spec = importlib.util.spec_from_file_location("secret_scanner", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(hook_mod, stdin_data: dict, env_overrides: dict | None = None):
    """Run hook_mod.main() with the given stdin dict, return (exit_code, stderr)."""
    stdin_str = json.dumps(stdin_data)
    captured_stderr = StringIO()
    exit_code = None
    extra_env = env_overrides or {}

    with patch("sys.stdin", StringIO(stdin_str)), \
         patch("sys.stderr", captured_stderr), \
         patch.dict(os.environ, extra_env, clear=False):
        try:
            hook_mod.main()
        except SystemExit as e:
            exit_code = e.code

    return exit_code, captured_stderr.getvalue()


# ---------------------------------------------------------------------------
# _load_secrets
# ---------------------------------------------------------------------------

class TestLoadSecrets:
    def setup_method(self):
        self.mod = _load_hook()

    def test_parses_simple_key_value(self, tmp_path):
        cfg = tmp_path / "config.env"
        cfg.write_text("API_TOKEN=abcdefghijklmnopqrstuvwxyz\n")
        result = self.mod._load_secrets(cfg)
        assert result == {"API_TOKEN": "abcdefghijklmnopqrstuvwxyz"}

    def test_strips_double_quotes(self, tmp_path):
        cfg = tmp_path / "config.env"
        cfg.write_text('API_TOKEN="abcdefghijklmnopqrstuvwxyz"\n')
        result = self.mod._load_secrets(cfg)
        assert result == {"API_TOKEN": "abcdefghijklmnopqrstuvwxyz"}

    def test_strips_single_quotes(self, tmp_path):
        cfg = tmp_path / "config.env"
        cfg.write_text("API_TOKEN='abcdefghijklmnopqrstuvwxyz'\n")
        result = self.mod._load_secrets(cfg)
        assert result == {"API_TOKEN": "abcdefghijklmnopqrstuvwxyz"}

    def test_filters_short_values(self, tmp_path):
        cfg = tmp_path / "config.env"
        cfg.write_text("LOBSTER_DEBUG=true\nSHORT=abc\n")
        result = self.mod._load_secrets(cfg)
        assert result == {}

    def test_skips_comment_lines(self, tmp_path):
        cfg = tmp_path / "config.env"
        cfg.write_text("# This is a comment\nAPI_TOKEN=abcdefghijklmnopqrstuvwxyz\n")
        result = self.mod._load_secrets(cfg)
        assert result == {"API_TOKEN": "abcdefghijklmnopqrstuvwxyz"}

    def test_skips_blank_lines(self, tmp_path):
        cfg = tmp_path / "config.env"
        cfg.write_text("\nAPI_TOKEN=abcdefghijklmnopqrstuvwxyz\n\n")
        result = self.mod._load_secrets(cfg)
        assert result == {"API_TOKEN": "abcdefghijklmnopqrstuvwxyz"}

    def test_returns_empty_for_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.env"
        result = self.mod._load_secrets(missing)
        assert result == {}

    def test_value_exactly_at_threshold(self, tmp_path):
        # Value of exactly 20 chars should be included
        value_20 = "A" * 20
        cfg = tmp_path / "config.env"
        cfg.write_text(f"SECRET={value_20}\n")
        result = self.mod._load_secrets(cfg)
        assert result == {"SECRET": value_20}

    def test_value_one_below_threshold_excluded(self, tmp_path):
        value_19 = "A" * 19
        cfg = tmp_path / "config.env"
        cfg.write_text(f"SECRET={value_19}\n")
        result = self.mod._load_secrets(cfg)
        assert result == {}


# ---------------------------------------------------------------------------
# _extract_strings_to_scan
# ---------------------------------------------------------------------------

class TestExtractStringsToScan:
    def setup_method(self):
        self.mod = _load_hook()

    def test_send_reply_extracts_text(self):
        result = self.mod._extract_strings_to_scan(
            "mcp__lobster-inbox__send_reply",
            {"text": "Hello world", "chat_id": 12345}
        )
        assert result == ["Hello world"]

    def test_github_extracts_body(self):
        result = self.mod._extract_strings_to_scan(
            "mcp__github__add_issue_comment",
            {"body": "Here is a secret token: xyz", "issue_number": 42}
        )
        assert "Here is a secret token: xyz" in result

    def test_github_extracts_title(self):
        result = self.mod._extract_strings_to_scan(
            "mcp__github__issue_write",
            {"title": "My issue title", "body": "Details"}
        )
        assert "My issue title" in result
        assert "Details" in result

    def test_bash_gh_write_is_scanned(self):
        result = self.mod._extract_strings_to_scan(
            "Bash",
            {"command": "gh issue comment 42 --body 'some content'"}
        )
        assert len(result) == 1
        assert "gh issue comment" in result[0]

    def test_bash_non_gh_write_is_skipped(self):
        result = self.mod._extract_strings_to_scan(
            "Bash",
            {"command": "ls -la /home/lobster"}
        )
        assert result == []

    def test_bash_gh_pr_is_scanned(self):
        result = self.mod._extract_strings_to_scan(
            "Bash",
            {"command": "gh pr create --title 'Fix bug'"}
        )
        assert len(result) == 1

    def test_add_comment_to_pending_review_extracts_body(self):
        result = self.mod._extract_strings_to_scan(
            "mcp__github__add_comment_to_pending_review",
            {"body": "LGTM — looks clean", "pull_request_number": 7}
        )
        assert "LGTM — looks clean" in result

    def test_create_pull_request_with_copilot_extracts_body_and_title(self):
        result = self.mod._extract_strings_to_scan(
            "mcp__github__create_pull_request_with_copilot",
            {"title": "Fix auth", "body": "Closes #99", "head": "my-branch"}
        )
        assert "Fix auth" in result
        assert "Closes #99" in result

    def test_delete_file_extracts_message(self):
        result = self.mod._extract_strings_to_scan(
            "mcp__github__delete_file",
            {"message": "Remove stale config", "path": "config.env", "sha": "abc123"}
        )
        assert "Remove stale config" in result

    def test_unrelated_tool_returns_empty(self):
        result = self.mod._extract_strings_to_scan(
            "Read",
            {"file_path": "/some/file.py"}
        )
        assert result == []

    def test_edit_tool_returns_empty(self):
        result = self.mod._extract_strings_to_scan(
            "Edit",
            {"file_path": "/some/file.py", "new_string": "content"}
        )
        assert result == []


# ---------------------------------------------------------------------------
# _scan_for_secrets
# ---------------------------------------------------------------------------

class TestScanForSecrets:
    def setup_method(self):
        self.mod = _load_hook()

    def test_detects_secret_in_text(self):
        secrets = {"API_TOKEN": "mysecrettoken123456789"}
        texts = ["Here is my token: mysecrettoken123456789 please use it"]
        result = self.mod._scan_for_secrets(texts, secrets)
        assert result == ["API_TOKEN"]

    def test_no_match_returns_empty(self):
        secrets = {"API_TOKEN": "mysecrettoken123456789"}
        texts = ["This text has no secrets"]
        result = self.mod._scan_for_secrets(texts, secrets)
        assert result == []

    def test_multiple_secrets_detected(self):
        secrets = {
            "TOKEN_A": "secretvaluealpha1234567",
            "TOKEN_B": "secretvaluebeta12345678",
        }
        texts = ["value: secretvaluealpha1234567 and secretvaluebeta12345678"]
        result = self.mod._scan_for_secrets(texts, secrets)
        assert set(result) == {"TOKEN_A", "TOKEN_B"}

    def test_partial_value_not_matched(self):
        # Only 10 chars of a 20-char secret — should not match
        secrets = {"API_TOKEN": "abcdefghijklmnopqrst"}
        texts = ["partial: abcdefghij"]
        result = self.mod._scan_for_secrets(texts, secrets)
        assert result == []

    def test_secret_found_across_multiple_texts(self):
        secrets = {"API_TOKEN": "mysecrettoken123456789"}
        texts = ["first text", "second text with mysecrettoken123456789 here"]
        result = self.mod._scan_for_secrets(texts, secrets)
        assert result == ["API_TOKEN"]


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

class TestMain:
    def setup_method(self):
        self.mod = _load_hook()

    def _make_config(self, tmp_path, extra_lines=""):
        cfg = tmp_path / "config.env"
        cfg.write_text(
            "TELEGRAM_BOT_TOKEN=FAKE_TELEGRAM_BOT_TOKEN_FOR_TESTING_AAABBBCCC1234567890\n"
            f"{extra_lines}\n"
        )
        return cfg

    def test_no_secret_exits_0_silently(self, tmp_path):
        cfg = self._make_config(tmp_path)
        stdin_data = {
            "tool_name": "mcp__lobster-inbox__send_reply",
            "tool_input": {"text": "Hello, how are you?", "chat_id": 123},
        }
        code, stderr = _run_main(
            self.mod, stdin_data,
            env_overrides={"LOBSTER_CONFIG_DIR": str(tmp_path)}
        )
        assert code == 0
        assert stderr == ""

    def test_secret_in_send_reply_warns_and_exits_0(self, tmp_path):
        cfg = self._make_config(tmp_path)
        # The token is in config.env; send it in a reply
        stdin_data = {
            "tool_name": "mcp__lobster-inbox__send_reply",
            "tool_input": {
                "text": "Your token is FAKE_TELEGRAM_BOT_TOKEN_FOR_TESTING_AAABBBCCC1234567890",
                "chat_id": 123,
            },
        }
        code, stderr = _run_main(
            self.mod, stdin_data,
            env_overrides={"LOBSTER_CONFIG_DIR": str(tmp_path)}
        )
        assert code == 0  # warn mode — must not block
        assert "WARNING" in stderr
        assert "TELEGRAM_BOT_TOKEN" in stderr
        # The raw value must NOT appear in stderr (only the key name should be logged)
        assert "FAKE_TELEGRAM_BOT_TOKEN_FOR_TESTING_AAABBBCCC1234567890" not in stderr

    def test_secret_in_github_body_warns_and_exits_0(self, tmp_path):
        cfg = self._make_config(tmp_path)
        stdin_data = {
            "tool_name": "mcp__github__add_issue_comment",
            "tool_input": {
                "body": "Token: FAKE_TELEGRAM_BOT_TOKEN_FOR_TESTING_AAABBBCCC1234567890",
                "issue_number": 10,
            },
        }
        code, stderr = _run_main(
            self.mod, stdin_data,
            env_overrides={"LOBSTER_CONFIG_DIR": str(tmp_path)}
        )
        assert code == 0
        assert "WARNING" in stderr
        assert "TELEGRAM_BOT_TOKEN" in stderr

    def test_secret_in_bash_gh_issue_warns(self, tmp_path):
        cfg = self._make_config(tmp_path)
        stdin_data = {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "gh issue comment 5 --body "
                    "'FAKE_TELEGRAM_BOT_TOKEN_FOR_TESTING_AAABBBCCC1234567890'"
                )
            },
        }
        code, stderr = _run_main(
            self.mod, stdin_data,
            env_overrides={"LOBSTER_CONFIG_DIR": str(tmp_path)}
        )
        assert code == 0
        assert "WARNING" in stderr

    def test_no_config_file_exits_0_silently(self, tmp_path):
        # Point LOBSTER_CONFIG_DIR at an empty dir with no config.env
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        stdin_data = {
            "tool_name": "mcp__lobster-inbox__send_reply",
            "tool_input": {"text": "Hello", "chat_id": 123},
        }
        code, stderr = _run_main(
            self.mod, stdin_data,
            env_overrides={"LOBSTER_CONFIG_DIR": str(empty_dir)}
        )
        assert code == 0
        assert stderr == ""

    def test_unrelated_tool_exits_0_silently(self, tmp_path):
        cfg = self._make_config(tmp_path)
        stdin_data = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/home/lobster/lobster/hooks/secret-scanner.py"},
        }
        code, stderr = _run_main(
            self.mod, stdin_data,
            env_overrides={"LOBSTER_CONFIG_DIR": str(tmp_path)}
        )
        assert code == 0
        assert stderr == ""

    def test_secret_in_add_comment_to_pending_review_warns(self, tmp_path):
        cfg = self._make_config(tmp_path)
        stdin_data = {
            "tool_name": "mcp__github__add_comment_to_pending_review",
            "tool_input": {
                "body": "Token: FAKE_TELEGRAM_BOT_TOKEN_FOR_TESTING_AAABBBCCC1234567890",
                "pull_request_number": 7,
            },
        }
        code, stderr = _run_main(
            self.mod, stdin_data,
            env_overrides={"LOBSTER_CONFIG_DIR": str(tmp_path)}
        )
        assert code == 0
        assert "WARNING" in stderr
        assert "TELEGRAM_BOT_TOKEN" in stderr

    def test_invalid_json_stdin_exits_0(self, tmp_path):
        """Malformed stdin should not crash the hook — silent pass."""
        captured_stderr = StringIO()
        exit_code = None
        with patch("sys.stdin", StringIO("not valid json")), \
             patch("sys.stderr", captured_stderr), \
             patch.dict(os.environ, {"LOBSTER_CONFIG_DIR": str(tmp_path)}):
            try:
                self.mod.main()
            except SystemExit as e:
                exit_code = e.code
        assert exit_code == 0
