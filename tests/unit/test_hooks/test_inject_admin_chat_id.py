"""
Unit tests for ADMIN_CHAT_ID injection in inject-bootup-context.py (issue #1976).

Problem: sys.dispatcher.bootup.md tells the dispatcher to find ADMIN_CHAT_ID via
  grep ADMIN_CHAT_ID ~/lobster-config/lobster.conf
But ~/lobster-config/lobster.conf does not exist. The value lives in
~/lobster-config/config.env as LOBSTER_ADMIN_CHAT_ID.

Fix: inject-bootup-context.py parses config.env at session start and prepends
a preamble line  ``ADMIN_CHAT_ID=<value>``  to the dispatcher's injected content.
The dispatcher can then read the value directly from its context without any
grep/file-read at startup.

Test coverage:
- _parse_admin_chat_id() returns the value when config.env contains LOBSTER_ADMIN_CHAT_ID
- _parse_admin_chat_id() returns None when config.env is absent
- _parse_admin_chat_id() returns None when LOBSTER_ADMIN_CHAT_ID is not in the file
- _parse_admin_chat_id() returns None when the value is empty/blank
- _parse_admin_chat_id() strips surrounding whitespace from the value
- _parse_admin_chat_id() handles OSError gracefully (returns None)
- Integration: dispatcher sessions get ADMIN_CHAT_ID preamble injected before bootup content
- Integration: subagent sessions do NOT get the preamble
- Integration: missing config.env does not break injection (graceful degradation)
- Integration: injected preamble appears before (not after) the dispatcher bootup content
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "inject-bootup-context.py"

# Named constant matching the variable name in config.env
LOBSTER_ADMIN_CHAT_ID_VAR = "LOBSTER_ADMIN_CHAT_ID"
SAMPLE_CHAT_ID = "8305714125"
PREAMBLE_PREFIX = "ADMIN_CHAT_ID="


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PatchEnv:
    """Context manager to temporarily set / restore environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved: dict = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _load_hook(*, workspace: Path) -> object:
    """Load inject-bootup-context.py with a controlled LOBSTER_WORKSPACE."""
    import uuid

    unique_name = f"inject_bootup_{uuid.uuid4().hex}"
    with _PatchEnv({"LOBSTER_WORKSPACE": str(workspace)}):
        spec = importlib.util.spec_from_file_location(unique_name, _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _write_config_env(config_dir: Path, content: str) -> Path:
    """Write a config.env file with the given content."""
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.env"
    config_file.write_text(content)
    return config_file


def _setup_bootup_files(tmp_path: Path) -> tuple[Path, Path]:
    """Write minimal dispatcher and subagent bootup stubs."""
    claude_dir = tmp_path / "lobster" / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    dispatcher_bootup = claude_dir / "sys.dispatcher.bootup.md"
    subagent_bootup = claude_dir / "sys.subagent.bootup.md"
    dispatcher_bootup.write_text("# DISPATCHER BOOTUP\n")
    subagent_bootup.write_text("# SUBAGENT BOOTUP\n")
    return dispatcher_bootup, subagent_bootup


def _run_hook(
    *,
    tmp_path: Path,
    config_env_path: Path | None,
    is_dispatcher: bool,
    session_id: str = "test-session-uuid",
) -> str:
    """Run main() and return stdout output as a string."""
    dispatcher_bootup, subagent_bootup = _setup_bootup_files(tmp_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    hook_input = json.dumps({"session_id": session_id})

    with _PatchEnv({"LOBSTER_WORKSPACE": str(tmp_path)}):
        spec = importlib.util.spec_from_file_location(
            f"inject_admin_chat_id_{session_id[:8]}", _HOOK_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Override file paths to use tmp_path
        mod.DISPATCHER_BOOTUP = dispatcher_bootup
        mod.SUBAGENT_BOOTUP = subagent_bootup
        mod.USER_BASE_BOOTUP = tmp_path / "no-user-base"
        mod.USER_DISPATCHER_BOOTUP = tmp_path / "no-user-dispatcher"
        mod.USER_SUBAGENT_BOOTUP = tmp_path / "no-user-subagent"

        if config_env_path is not None:
            mod.CONFIG_ENV_PATH = config_env_path
        else:
            # Point to a non-existent path
            mod.CONFIG_ENV_PATH = tmp_path / "nonexistent" / "config.env"

        # Control dispatcher detection directly
        mod._is_startup_flag_dispatcher = lambda: is_dispatcher
        if is_dispatcher:
            mod._consume_startup_flag = lambda: None

        out_buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(hook_input)):
            with redirect_stdout(out_buf):
                with pytest.raises(SystemExit):
                    mod.main()

    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# Unit tests for _parse_admin_chat_id()
# ---------------------------------------------------------------------------


class TestParseAdminChatId:
    """Unit tests for the _parse_admin_chat_id() helper."""

    def test_returns_value_when_key_present(self, tmp_path):
        """Returns the value when LOBSTER_ADMIN_CHAT_ID is present in config.env."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"TELEGRAM_BOT_TOKEN=fake-token\n{LOBSTER_ADMIN_CHAT_ID_VAR}={SAMPLE_CHAT_ID}\n",
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result == SAMPLE_CHAT_ID

    def test_returns_none_when_file_absent(self, tmp_path):
        """Returns None when config.env does not exist."""
        absent_path = tmp_path / "no-config" / "config.env"
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(absent_path)
        assert result is None

    def test_returns_none_when_key_absent(self, tmp_path):
        """Returns None when LOBSTER_ADMIN_CHAT_ID is not in the file."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            "TELEGRAM_BOT_TOKEN=fake-token\nSOME_OTHER_KEY=value\n",
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result is None

    def test_returns_none_when_value_is_empty(self, tmp_path):
        """Returns None when LOBSTER_ADMIN_CHAT_ID= has an empty value."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}=\n",
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result is None

    def test_returns_none_when_value_is_blank(self, tmp_path):
        """Returns None when LOBSTER_ADMIN_CHAT_ID= has only whitespace."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}=   \n",
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result is None

    def test_strips_whitespace_from_value(self, tmp_path):
        """Strips surrounding whitespace from the parsed value."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}=  {SAMPLE_CHAT_ID}  \n",
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result == SAMPLE_CHAT_ID

    def test_returns_none_on_oserror(self, tmp_path):
        """Returns None on OSError reading the file (safe default)."""
        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.read_text.side_effect = OSError("permission denied")

        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(mock_path)
        assert result is None

    def test_handles_comment_lines(self, tmp_path):
        """Skips comment lines (lines starting with #) and finds the value."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"# This is a comment\n{LOBSTER_ADMIN_CHAT_ID_VAR}={SAMPLE_CHAT_ID}\n",
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result == SAMPLE_CHAT_ID

    def test_handles_file_without_trailing_newline(self, tmp_path):
        """Parses the value correctly even without a trailing newline."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}={SAMPLE_CHAT_ID}",  # no trailing newline
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result == SAMPLE_CHAT_ID

    def test_does_not_match_key_with_longer_prefix(self, tmp_path):
        """Does not falsely match a key that starts with LOBSTER_ADMIN_CHAT_ID but has a suffix.

        e.g. LOBSTER_ADMIN_CHAT_ID_BACKUP=999 must not be returned when
        LOBSTER_ADMIN_CHAT_ID itself is absent.
        """
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}_BACKUP=999\n",  # similar but different key
        )
        mod = _load_hook(workspace=tmp_path)
        result = mod._parse_admin_chat_id(config_file)
        assert result is None, (
            "Should not match LOBSTER_ADMIN_CHAT_ID_BACKUP when LOBSTER_ADMIN_CHAT_ID is absent"
        )


# ---------------------------------------------------------------------------
# Integration tests: preamble injection in main()
# ---------------------------------------------------------------------------


class TestAdminChatIdInjectionInMain:
    """main() injects ADMIN_CHAT_ID preamble for dispatcher sessions only."""

    def test_dispatcher_gets_preamble_before_bootup_content(self, tmp_path):
        """Dispatcher session: preamble line appears before bootup content."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}={SAMPLE_CHAT_ID}\n",
        )
        output = _run_hook(
            tmp_path=tmp_path,
            config_env_path=config_file,
            is_dispatcher=True,
        )

        preamble_line = f"{PREAMBLE_PREFIX}{SAMPLE_CHAT_ID}"
        assert preamble_line in output, (
            f"Dispatcher output must contain '{preamble_line}'"
        )
        assert "DISPATCHER BOOTUP" in output, "Dispatcher bootup content must still be injected"

        # Preamble must appear before the bootup content
        preamble_pos = output.index(preamble_line)
        bootup_pos = output.index("DISPATCHER BOOTUP")
        assert preamble_pos < bootup_pos, (
            "ADMIN_CHAT_ID preamble must appear before the dispatcher bootup content"
        )

    def test_subagent_does_not_get_preamble(self, tmp_path):
        """Subagent session: preamble is NOT injected."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}={SAMPLE_CHAT_ID}\n",
        )
        output = _run_hook(
            tmp_path=tmp_path,
            config_env_path=config_file,
            is_dispatcher=False,
        )

        assert PREAMBLE_PREFIX not in output, (
            "Subagent sessions must NOT receive the ADMIN_CHAT_ID preamble"
        )
        assert "SUBAGENT BOOTUP" in output, "Subagent bootup content must still be injected"

    def test_dispatcher_without_config_env_still_injects_bootup(self, tmp_path):
        """Missing config.env: graceful degradation — bootup still injected, no preamble."""
        output = _run_hook(
            tmp_path=tmp_path,
            config_env_path=None,  # config.env absent
            is_dispatcher=True,
        )

        assert "DISPATCHER BOOTUP" in output, (
            "Dispatcher bootup must still be injected even when config.env is absent"
        )
        assert PREAMBLE_PREFIX not in output, (
            "No ADMIN_CHAT_ID preamble when config.env is absent"
        )

    def test_dispatcher_without_key_in_config_env_still_injects_bootup(self, tmp_path):
        """config.env exists but lacks LOBSTER_ADMIN_CHAT_ID: no preamble, bootup still runs."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            "TELEGRAM_BOT_TOKEN=fake-token\n",  # no LOBSTER_ADMIN_CHAT_ID
        )
        output = _run_hook(
            tmp_path=tmp_path,
            config_env_path=config_file,
            is_dispatcher=True,
        )

        assert "DISPATCHER BOOTUP" in output
        assert PREAMBLE_PREFIX not in output, (
            "No preamble when LOBSTER_ADMIN_CHAT_ID is absent from config.env"
        )

    def test_preamble_contains_exact_chat_id(self, tmp_path):
        """Preamble line contains the exact numeric value from config.env."""
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}={SAMPLE_CHAT_ID}\n",
        )
        output = _run_hook(
            tmp_path=tmp_path,
            config_env_path=config_file,
            is_dispatcher=True,
        )

        expected_line = f"{PREAMBLE_PREFIX}{SAMPLE_CHAT_ID}"
        assert expected_line in output, (
            f"Expected preamble '{expected_line}' in output"
        )

    def test_preamble_annotation_appears_in_dispatcher_output(self, tmp_path):
        """Dispatcher output includes the annotation comment explaining the injection source.

        The annotation tells the dispatcher that ADMIN_CHAT_ID was injected from
        config.env so it knows not to grep for it at startup.
        """
        config_file = _write_config_env(
            tmp_path / "lobster-config",
            f"{LOBSTER_ADMIN_CHAT_ID_VAR}={SAMPLE_CHAT_ID}\n",
        )
        output = _run_hook(
            tmp_path=tmp_path,
            config_env_path=config_file,
            is_dispatcher=True,
        )

        assert "config.env" in output, (
            "Annotation must mention config.env as the injection source"
        )
        assert "no grep needed" in output.lower(), (
            "Annotation must confirm no grep is needed at startup"
        )
