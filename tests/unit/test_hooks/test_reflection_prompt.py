"""
Unit tests for _schedule_reflection_prompt() in on-compact.py and on-fresh-start.py.

Verifies (Fix B, issue #1998):
- In debug mode, writes a well-formed reflection prompt to the sidecar file
  (~/messages/bootup-prompt.md) -- NOT to the inbox.
- In non-debug mode, writes nothing.
- Written content contains expected key phrases and the trigger name.
- Atomic write (no .tmp file left behind).
- Silent on filesystem errors (never crashes the hook).
- Second call overwrites the sidecar file (not appends / accumulates).
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _PatchEnv:
    """Context manager to temporarily set / unset environment variables."""

    def __init__(self, env: dict):
        self._env = env
        self._saved = {}

    def __enter__(self):
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, saved_v in self._saved.items():
            if saved_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_v


def _make_session_role_stub(is_dispatcher: bool = True):
    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: is_dispatcher
    stub.DISPATCHER_SESSION_FILE = Path("/tmp/lobster-test-dispatcher-session")
    stub.write_dispatcher_session_id = lambda sid: None
    stub._read_dispatcher_session_id = lambda: None
    return stub


def _load_on_compact(
    bootup_prompt_file: str = None,
    compaction_state_override: str = None,
):
    """Load hooks/on-compact.py as a module, with isolated file paths."""
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override
    if bootup_prompt_file:
        env_patch["LOBSTER_BOOTUP_PROMPT_FILE_OVERRIDE"] = bootup_prompt_file

    hook_path = _HOOKS_DIR / "on-compact.py"
    # Save and restore session_role to avoid polluting other tests that rely on the
    # real session_role module.
    saved_session_role = sys.modules.get("session_role")
    try:
        with _PatchEnv(env_patch):
            spec = importlib.util.spec_from_file_location(
                f"on_compact_{os.getpid()}_{id(bootup_prompt_file)}", hook_path
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["session_role"] = _make_session_role_stub()
            spec.loader.exec_module(mod)
    finally:
        # Restore the previous session_role (or remove if it was not there)
        if saved_session_role is None:
            sys.modules.pop("session_role", None)
        else:
            sys.modules["session_role"] = saved_session_role

    if bootup_prompt_file:
        mod.BOOTUP_PROMPT_FILE = Path(bootup_prompt_file)
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    return mod


def _load_on_fresh_start(
    bootup_prompt_file: str = None,
    compaction_state_override: str = None,
):
    """Load hooks/on-fresh-start.py as a module, with isolated file paths."""
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override
    if bootup_prompt_file:
        env_patch["LOBSTER_BOOTUP_PROMPT_FILE_OVERRIDE"] = bootup_prompt_file

    hook_path = _HOOKS_DIR / "on-fresh-start.py"
    saved_session_role = sys.modules.get("session_role")
    try:
        with _PatchEnv(env_patch):
            spec = importlib.util.spec_from_file_location(
                f"on_fresh_start_{os.getpid()}_{id(bootup_prompt_file)}", hook_path
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["session_role"] = _make_session_role_stub()
            spec.loader.exec_module(mod)
    finally:
        if saved_session_role is None:
            sys.modules.pop("session_role", None)
        else:
            sys.modules["session_role"] = saved_session_role

    if bootup_prompt_file:
        mod.BOOTUP_PROMPT_FILE = Path(bootup_prompt_file)
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    return mod


# ---------------------------------------------------------------------------
# Tests: on-compact.py
# ---------------------------------------------------------------------------

class TestScheduleReflectionPromptCompact:
    """Tests for _schedule_reflection_prompt() in on-compact.py (sidecar, Fix B)."""

    def test_writes_sidecar_file_in_debug_mode(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        assert sidecar.exists(), "sidecar file was not written in debug mode"

    def test_does_not_write_to_inbox(self, tmp_path):
        """No inbox file should be written -- sidecar only."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))
        mod.INBOX_DIR = inbox

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        inbox_files = list(inbox.iterdir())
        assert len(inbox_files) == 0, f"unexpected inbox files: {[f.name for f in inbox_files]}"

    def test_does_not_write_in_non_debug_mode(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "false"}):
            mod._schedule_reflection_prompt("compaction")

        assert not sidecar.exists()

    def test_does_not_write_when_debug_env_absent(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": ""}):
            mod._schedule_reflection_prompt("compaction")

        assert not sidecar.exists()

    def test_written_content_contains_trigger_and_key_phrases(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        text = sidecar.read_text()
        assert "compaction" in text.lower()
        assert "friction" in text.lower() or "observations" in text.lower()
        assert "SiderealPress/lobster" in text

    def test_no_tmp_file_left_behind(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0, f"tmp files left behind: {tmp_files}"

    def test_creates_parent_dir_if_absent(self, tmp_path):
        sidecar = tmp_path / "subdir_not_created_yet" / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        assert sidecar.exists()

    def test_overwrites_on_second_call(self, tmp_path):
        """Second call must overwrite, not append."""
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")
            first_content = sidecar.read_text()
            mod._schedule_reflection_prompt("compaction")
            second_content = sidecar.read_text()

        assert second_content == first_content  # same content, not doubled

    def test_silent_on_write_failure(self):
        """Must not raise when the sidecar path is not writable."""
        mod = _load_on_compact(
            bootup_prompt_file="/proc/lobster_test_nonexistent/bootup-prompt.md"
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            # Must not raise
            mod._schedule_reflection_prompt("compaction")


# ---------------------------------------------------------------------------
# Tests: on-fresh-start.py
# ---------------------------------------------------------------------------

class TestScheduleReflectionPromptFreshStart:
    """Tests for _schedule_reflection_prompt() in on-fresh-start.py (sidecar, Fix B)."""

    def test_writes_sidecar_file_in_debug_mode(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        state_file = tmp_path / "compaction-state.json"
        mod = _load_on_fresh_start(
            bootup_prompt_file=str(sidecar),
            compaction_state_override=str(state_file),
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        assert sidecar.exists(), "sidecar file was not written in debug mode"

    def test_does_not_write_to_inbox(self, tmp_path):
        """No inbox file should be written -- sidecar only."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))
        mod.INBOX_DIR = inbox

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        inbox_files = list(inbox.iterdir())
        assert len(inbox_files) == 0, f"unexpected inbox files: {[f.name for f in inbox_files]}"

    def test_does_not_write_in_non_debug_mode(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "false"}):
            mod._schedule_reflection_prompt("bootup")

        assert not sidecar.exists()

    def test_bootup_trigger_appears_in_content(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        text = sidecar.read_text()
        assert "bootup" in text.lower()

    def test_written_content_contains_key_phrases(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        text = sidecar.read_text()
        assert "friction" in text.lower() or "observations" in text.lower()
        assert "SiderealPress/lobster" in text

    def test_no_tmp_file_left_behind(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_silent_on_write_failure(self):
        mod = _load_on_fresh_start(
            bootup_prompt_file="/proc/lobster_test_nonexistent/bootup-prompt.md"
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            # Must not raise
            mod._schedule_reflection_prompt("bootup")
