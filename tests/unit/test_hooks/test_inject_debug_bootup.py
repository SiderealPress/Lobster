"""
Unit tests for hooks/inject-debug-bootup.py

Tests cover:
- Hook exits silently when LOBSTER_DEBUG is not set
- Hook exits silently when LOBSTER_DEBUG is set to a non-true value
- Hook injects file content to stdout when LOBSTER_DEBUG=true (env var)
- Hook injects file content to stdout when LOBSTER_DEBUG=true (config.env)
- Hook handles case-insensitive LOBSTER_DEBUG values (TRUE, True)
- Hook exits silently (with stderr warning) when the bootup file is missing
- Hook exits silently (with stderr warning) when the bootup file is unreadable
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_hook(monkeypatch, tmp_path):
    """
    Load hooks/inject-debug-bootup.py as a module, with CONFIG_ENV and
    DEBUG_BOOTUP_FILE patched to temp-dir paths so tests are hermetic.

    Returns the loaded module object.
    """
    hook_path = Path(__file__).parents[3] / "hooks" / "inject-debug-bootup.py"
    spec = importlib.util.spec_from_file_location("inject_debug_bootup", hook_path)
    mod = importlib.util.module_from_spec(spec)
    # Load into a fresh namespace each time
    spec.loader.exec_module(mod)

    # Patch module-level path constants to point at tmp_path
    config_env = tmp_path / "config.env"
    debug_file = tmp_path / "debug.sys.bootup.md"
    monkeypatch.setattr(mod, "CONFIG_ENV", config_env)
    monkeypatch.setattr(mod, "DEBUG_BOOTUP_FILE", debug_file)

    return mod, config_env, debug_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInjectDebugBootup:
    def test_no_debug_env_exits_silently(self, monkeypatch, tmp_path, capsys):
        """When LOBSTER_DEBUG is not set, hook exits 0 and prints nothing."""
        monkeypatch.delenv("LOBSTER_DEBUG", raising=False)
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)
        # config.env absent → no fallback

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_debug_false_exits_silently(self, monkeypatch, tmp_path, capsys):
        """When LOBSTER_DEBUG=false, hook exits 0 and prints nothing."""
        monkeypatch.setenv("LOBSTER_DEBUG", "false")
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_debug_true_env_injects_content(self, monkeypatch, tmp_path, capsys):
        """When LOBSTER_DEBUG=true (env), hook prints bootup file to stdout."""
        monkeypatch.setenv("LOBSTER_DEBUG", "true")
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        expected = "# Debug Mode\n\nSome debug instructions here.\n"
        debug_file.write_text(expected)

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == expected.strip()
        assert captured.err == ""

    def test_debug_true_case_insensitive(self, monkeypatch, tmp_path, capsys):
        """LOBSTER_DEBUG=TRUE (uppercase) should also trigger injection."""
        monkeypatch.setenv("LOBSTER_DEBUG", "TRUE")
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        debug_file.write_text("# Debug content")

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "# Debug content" in captured.out

    def test_debug_true_mixed_case(self, monkeypatch, tmp_path, capsys):
        """LOBSTER_DEBUG=True (mixed case) should also trigger injection."""
        monkeypatch.setenv("LOBSTER_DEBUG", "True")
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        debug_file.write_text("# Debug content")

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "# Debug content" in captured.out

    def test_debug_true_from_config_env(self, monkeypatch, tmp_path, capsys):
        """When LOBSTER_DEBUG is unset but config.env has LOBSTER_DEBUG=true, inject."""
        monkeypatch.delenv("LOBSTER_DEBUG", raising=False)
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        config_env.write_text("LOBSTER_DEBUG=true\n")
        debug_file.write_text("# From config.env")

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "# From config.env" in captured.out

    def test_debug_true_config_env_quoted(self, monkeypatch, tmp_path, capsys):
        """config.env value with quotes (LOBSTER_DEBUG=\"true\") is handled."""
        monkeypatch.delenv("LOBSTER_DEBUG", raising=False)
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        config_env.write_text('LOBSTER_DEBUG="true"\n')
        debug_file.write_text("# Quoted")

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "# Quoted" in captured.out

    def test_env_overrides_config_env_false(self, monkeypatch, tmp_path, capsys):
        """Env var LOBSTER_DEBUG=false takes precedence over config.env true."""
        monkeypatch.setenv("LOBSTER_DEBUG", "false")
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        # config.env says true, but env var says false — env var wins
        config_env.write_text("LOBSTER_DEBUG=true\n")
        debug_file.write_text("# Should not be injected")

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_missing_bootup_file_exits_with_warning(self, monkeypatch, tmp_path, capsys):
        """When LOBSTER_DEBUG=true but bootup file is missing, exit 0 with stderr warning."""
        monkeypatch.setenv("LOBSTER_DEBUG", "true")
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)
        # debug_file deliberately not created

        with pytest.raises(SystemExit) as exc_info:
            mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "WARNING" in captured.err
        assert "not found" in captured.err

    def test_unreadable_bootup_file_exits_with_warning(self, monkeypatch, tmp_path, capsys):
        """When bootup file exists but is unreadable, exit 0 with stderr warning."""
        monkeypatch.setenv("LOBSTER_DEBUG", "true")
        mod, config_env, debug_file = _load_hook(monkeypatch, tmp_path)

        debug_file.write_text("content")
        debug_file.chmod(0o000)  # remove read permission

        try:
            with pytest.raises(SystemExit) as exc_info:
                mod.main()

            assert exc_info.value.code == 0
            captured = capsys.readouterr()
            assert captured.out == ""
            assert "WARNING" in captured.err
        finally:
            debug_file.chmod(0o644)  # restore so tmp_path cleanup works
