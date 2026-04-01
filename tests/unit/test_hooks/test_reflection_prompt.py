"""
Unit tests for _schedule_reflection_prompt() in on-compact.py and on-fresh-start.py.

Verifies:
- In debug mode, writes a well-formed reflection_prompt message to the inbox
- In non-debug mode, writes nothing
- Written message has expected fields and content
- Atomic write (no .tmp file left behind)
- Silent on filesystem errors (never crashes the hook)
"""

import importlib.util
import json
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


def _load_on_compact(inbox_dir: str = None, compaction_state_override: str = None):
    """Load hooks/on-compact.py as a module, with isolated file paths."""
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override

    hook_path = _HOOKS_DIR / "on-compact.py"
    with _PatchEnv(env_patch):
        spec = importlib.util.spec_from_file_location("on_compact", hook_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("session_role", _make_session_role_stub())
        spec.loader.exec_module(mod)

    if inbox_dir:
        mod.INBOX_DIR = Path(inbox_dir)
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    return mod


def _load_on_fresh_start(inbox_dir: str = None, compaction_state_override: str = None):
    """Load hooks/on-fresh-start.py as a module, with isolated file paths."""
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override

    hook_path = _HOOKS_DIR / "on-fresh-start.py"
    with _PatchEnv(env_patch):
        spec = importlib.util.spec_from_file_location("on_fresh_start", hook_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("session_role", _make_session_role_stub())
        spec.loader.exec_module(mod)

    if inbox_dir:
        mod.INBOX_DIR = Path(inbox_dir)
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    return mod


# ---------------------------------------------------------------------------
# Tests: on-compact.py
# ---------------------------------------------------------------------------

class TestScheduleReflectionPromptCompact:
    """Tests for _schedule_reflection_prompt() in on-compact.py."""

    def test_writes_inbox_file_in_debug_mode(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_compact(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        assert len(files) == 1, f"expected 1 file, got {[f.name for f in files]}"

    def test_does_not_write_in_non_debug_mode(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_compact(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "false"}):
            mod._schedule_reflection_prompt("compaction")

        files = list(inbox.iterdir())
        assert len(files) == 0

    def test_does_not_write_when_debug_env_absent(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_compact(inbox_dir=str(inbox))

        env_without_debug = {k: v for k, v in os.environ.items() if k != "LOBSTER_DEBUG"}
        with _PatchEnv({"LOBSTER_DEBUG": ""}):
            mod._schedule_reflection_prompt("compaction")

        files = list(inbox.iterdir())
        assert len(files) == 0

    def test_written_message_has_correct_type_and_trigger(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_compact(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        data = json.loads(files[0].read_text())
        assert data["type"] == "reflection_prompt"
        assert data["trigger"] == "compaction"
        assert data["source"] == "system"
        assert data["chat_id"] == 0

    def test_written_message_content_contains_key_phrases(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_compact(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        data = json.loads(files[0].read_text())
        text = data["text"]
        assert "friction" in text.lower() or "observations" in text.lower()
        assert "SiderealPress/lobster" in text

    def test_no_tmp_file_left_behind(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_compact(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        tmp_files = [f for f in inbox.iterdir() if f.suffix == ".tmp"]
        assert len(tmp_files) == 0, f"tmp files left behind: {tmp_files}"

    def test_creates_inbox_dir_if_absent(self, tmp_path):
        inbox = tmp_path / "inbox_not_created_yet"
        mod = _load_on_compact(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        assert inbox.exists()
        files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        assert len(files) == 1

    def test_silent_on_write_failure(self, tmp_path):
        """Must not raise when the inbox path is not writable."""
        mod = _load_on_compact(inbox_dir="/proc/lobster_test_nonexistent/inbox")

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            # Must not raise
            mod._schedule_reflection_prompt("compaction")


# ---------------------------------------------------------------------------
# Tests: on-fresh-start.py
# ---------------------------------------------------------------------------

class TestScheduleReflectionPromptFreshStart:
    """Tests for _schedule_reflection_prompt() in on-fresh-start.py."""

    def test_writes_inbox_file_in_debug_mode(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        state_file = tmp_path / "compaction-state.json"
        mod = _load_on_fresh_start(
            inbox_dir=str(inbox),
            compaction_state_override=str(state_file),
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        assert len(files) == 1

    def test_does_not_write_in_non_debug_mode(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "false"}):
            mod._schedule_reflection_prompt("bootup")

        files = list(inbox.iterdir())
        assert len(files) == 0

    def test_bootup_trigger_in_message(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        data = json.loads(files[0].read_text())
        assert data["trigger"] == "bootup"
        assert data["type"] == "reflection_prompt"

    def test_no_tmp_file_left_behind(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start(inbox_dir=str(inbox))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        tmp_files = [f for f in inbox.iterdir() if f.suffix == ".tmp"]
        assert len(tmp_files) == 0

    def test_silent_on_write_failure(self):
        mod = _load_on_fresh_start(inbox_dir="/proc/lobster_test_nonexistent/inbox")

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            # Must not raise
            mod._schedule_reflection_prompt("bootup")
