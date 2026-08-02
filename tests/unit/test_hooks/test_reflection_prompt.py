"""
Unit tests for _schedule_reflection_prompt() in on-compact.py and on-fresh-start.py.

Issue #1998: reflection prompts used to flow through the inbox as regular
messages, costing the dispatcher 2 extra MCP round-trips per restart
(mark_processing + mark_processed) just to read a one-shot debug prompt once
and discard it. The fix writes the prompt to a sidecar file
(~/messages/bootup-prompt.md) instead -- the dispatcher reads and deletes it
directly at startup (one Read call, see sys.dispatcher.bootup.md step 2e).

Verifies:
- In debug mode, writes the prompt to the sidecar file (not the inbox)
- In non-debug mode, writes nothing
- Sidecar content contains the expected trigger and key phrases
- Atomic write (no .tmp file left behind)
- A second call (e.g. a later restart) overwrites the sidecar file rather
  than accumulating -- there is no dedup bookkeeping needed since only the
  most recent prompt is ever meaningful
- Silent on filesystem errors (never crashes the hook)
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

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


def _load_on_compact(bootup_prompt_file: str = None, compaction_state_override: str = None):
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

    if bootup_prompt_file:
        mod.BOOTUP_PROMPT_FILE = Path(bootup_prompt_file)
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    return mod


def _load_on_fresh_start(bootup_prompt_file: str = None, compaction_state_override: str = None):
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

    if bootup_prompt_file:
        mod.BOOTUP_PROMPT_FILE = Path(bootup_prompt_file)
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    return mod


# ---------------------------------------------------------------------------
# Tests: on-compact.py
# ---------------------------------------------------------------------------

class TestScheduleReflectionPromptCompact:
    """Tests for _schedule_reflection_prompt() in on-compact.py."""

    def test_writes_sidecar_file_in_debug_mode(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        assert sidecar.exists(), "sidecar file should be written in debug mode"

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

    def test_sidecar_content_contains_trigger_and_key_phrases(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        text = sidecar.read_text()
        assert "Compaction" in text
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
        sidecar = tmp_path / "not_created_yet" / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")

        assert sidecar.exists()

    def test_second_call_overwrites_rather_than_accumulates(self, tmp_path):
        """A later restart's prompt replaces the previous one -- no dedup
        bookkeeping needed, since only the most recent prompt matters (issue
        #1998's sidecar design, unlike the old inbox path's ID-based dedup).
        """
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_compact(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("compaction")
            mod._schedule_reflection_prompt("compaction")

        # Still exactly one sidecar file (overwritten), not two, and no leftover
        # .tmp artifact from either write.
        assert sidecar.exists()
        matches = list(sidecar.parent.glob("bootup-prompt.md*"))
        assert matches == [sidecar], f"expected only the sidecar file, got: {matches}"

    def test_silent_on_write_failure(self):
        """Must not raise when the sidecar path is not writable."""
        mod = _load_on_compact(bootup_prompt_file="/proc/lobster_test_nonexistent/bootup-prompt.md")

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            # Must not raise
            mod._schedule_reflection_prompt("compaction")


# ---------------------------------------------------------------------------
# Tests: on-fresh-start.py
# ---------------------------------------------------------------------------

class TestScheduleReflectionPromptFreshStart:
    """Tests for _schedule_reflection_prompt() in on-fresh-start.py."""

    def test_writes_sidecar_file_in_debug_mode(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        state_file = tmp_path / "compaction-state.json"
        mod = _load_on_fresh_start(
            bootup_prompt_file=str(sidecar),
            compaction_state_override=str(state_file),
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        assert sidecar.exists()

    def test_does_not_write_in_non_debug_mode(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "false"}):
            mod._schedule_reflection_prompt("bootup")

        assert not sidecar.exists()

    def test_bootup_trigger_in_sidecar_content(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        text = sidecar.read_text()
        assert "Bootup" in text

    def test_no_tmp_file_left_behind(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_second_call_overwrites_rather_than_accumulates(self, tmp_path):
        sidecar = tmp_path / "bootup-prompt.md"
        mod = _load_on_fresh_start(bootup_prompt_file=str(sidecar))

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")
            mod._schedule_reflection_prompt("bootup")

        matches = list(sidecar.parent.glob("bootup-prompt.md*"))
        assert matches == [sidecar], f"expected only the sidecar file, got: {matches}"

    def test_silent_on_write_failure(self):
        mod = _load_on_fresh_start(bootup_prompt_file="/proc/lobster_test_nonexistent/bootup-prompt.md")

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            # Must not raise
            mod._schedule_reflection_prompt("bootup")


# ---------------------------------------------------------------------------
# Regression guards: the old inbox-message path must be gone
# ---------------------------------------------------------------------------

class TestNoInboxDedupMachinery:
    """_reflection_already_exists() was only needed for the old inbox-message
    dedup path (issue #2039). The sidecar file overwrites unconditionally, so
    this function -- and the ID-based dedup it enabled -- should no longer
    exist in either hook (issue #1998 cleanup).
    """

    def test_on_compact_has_no_reflection_already_exists(self, tmp_path):
        mod = _load_on_compact(bootup_prompt_file=str(tmp_path / "bootup-prompt.md"))
        assert not hasattr(mod, "_reflection_already_exists"), (
            "on-compact.py still defines _reflection_already_exists -- this ID-based "
            "dedup machinery was only needed for the old inbox-message path and should "
            "be removed now that the sidecar file overwrites unconditionally (issue #1998)."
        )

    def test_on_fresh_start_has_no_reflection_already_exists(self, tmp_path):
        mod = _load_on_fresh_start(bootup_prompt_file=str(tmp_path / "bootup-prompt.md"))
        assert not hasattr(mod, "_reflection_already_exists"), (
            "on-fresh-start.py still defines _reflection_already_exists -- this ID-based "
            "dedup machinery was only needed for the old inbox-message path and should "
            "be removed now that the sidecar file overwrites unconditionally (issue #1998)."
        )
