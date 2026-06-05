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


def _load_on_compact(
    inbox_dir: str = None,
    compaction_state_override: str = None,
    processing_dir: str = None,
    processed_dir: str = None,
):
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
    if processing_dir:
        mod.PROCESSING_DIR = Path(processing_dir)
    if processed_dir:
        mod.PROCESSED_DIR = Path(processed_dir)
    return mod


def _load_on_fresh_start(
    inbox_dir: str = None,
    compaction_state_override: str = None,
    processing_dir: str = None,
    processed_dir: str = None,
):
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
    if processing_dir:
        mod.PROCESSING_DIR = Path(processing_dir)
    if processed_dir:
        mod.PROCESSED_DIR = Path(processed_dir)
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


# ---------------------------------------------------------------------------
# Fix #2039 — dedup tests for concurrent hook invocations
# ---------------------------------------------------------------------------

class TestReflectionDedup:
    """Tests for _reflection_already_exists() and the dedup path in
    _schedule_reflection_prompt() — both hooks.

    Issue #2039: concurrent hook invocations within the same second share the
    same msg_id (1-second precision) but write different filenames.  The first
    invocation must win; subsequent ones must skip silently, leaving exactly
    one file in the inbox.
    """

    # -- on-compact.py --

    def test_compact_dedup_skips_when_id_already_in_inbox(self, tmp_path):
        """Second call with same timestamp skips writing when first file is in inbox/."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        processed = tmp_path / "processed"
        processed.mkdir()

        mod = _load_on_compact(
            inbox_dir=str(inbox),
            processing_dir=str(processing),
            processed_dir=str(processed),
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            # First call — writes the file.
            mod._schedule_reflection_prompt("compaction")
            first_files = [f for f in inbox.iterdir() if f.suffix == ".json"]
            assert len(first_files) == 1, "first call must write exactly one file"

            existing = json.loads(first_files[0].read_text())
            existing_id = existing["id"]

            # Call again with the same second → same msg_id → should skip.
            mod._schedule_reflection_prompt("compaction")

        all_files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        ids = [json.loads(f.read_text()).get("id") for f in all_files]
        assert ids.count(existing_id) == 1, (
            f"expected exactly 1 file with id={existing_id!r}, got ids={ids}"
        )

    def test_compact_dedup_skips_when_id_in_processing(self, tmp_path):
        """Dedup check finds an existing file in processing/ and skips."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        processed = tmp_path / "processed"
        processed.mkdir()

        import time as _time
        ts = _time.time()
        msg_id = f"reflection_compaction_{int(ts)}"
        existing_msg = {
            "id": msg_id,
            "type": "reflection_prompt",
            "trigger": "compaction",
            "source": "system",
            "text": "already here",
        }
        (processing / f"{int(ts * 1000)}_reflection_compaction.json").write_text(
            json.dumps(existing_msg)
        )

        mod = _load_on_compact(
            inbox_dir=str(inbox),
            processing_dir=str(processing),
            processed_dir=str(processed),
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            with _PatchTime(int(ts)):
                mod._schedule_reflection_prompt("compaction")

        new_files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        assert len(new_files) == 0, (
            "expected no new inbox file when existing file is in processing/"
        )

    def test_compact_dedup_skips_when_id_in_processed(self, tmp_path):
        """Dedup check finds an already-processed file and skips re-writing."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        processed = tmp_path / "processed"
        processed.mkdir()

        import time as _time
        ts = _time.time()
        msg_id = f"reflection_compaction_{int(ts)}"
        existing_msg = {
            "id": msg_id,
            "type": "reflection_prompt",
            "trigger": "compaction",
            "source": "system",
            "text": "already processed",
        }
        (processed / f"{int(ts * 1000)}_reflection_compaction.json").write_text(
            json.dumps(existing_msg)
        )

        mod = _load_on_compact(
            inbox_dir=str(inbox),
            processing_dir=str(processing),
            processed_dir=str(processed),
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            with _PatchTime(int(ts)):
                mod._schedule_reflection_prompt("compaction")

        new_files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        assert len(new_files) == 0, (
            "expected no new inbox file when reflection was already processed"
        )

    # -- on-fresh-start.py --

    def test_freshstart_dedup_skips_when_id_already_in_inbox(self, tmp_path):
        """Second call with same timestamp skips writing when first file is in inbox/."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        processed = tmp_path / "processed"
        processed.mkdir()

        mod = _load_on_fresh_start(
            inbox_dir=str(inbox),
            processing_dir=str(processing),
            processed_dir=str(processed),
        )

        with _PatchEnv({"LOBSTER_DEBUG": "true"}):
            mod._schedule_reflection_prompt("bootup")
            first_files = [f for f in inbox.iterdir() if f.suffix == ".json"]
            assert len(first_files) == 1, "first call must write exactly one file"

            existing = json.loads(first_files[0].read_text())
            existing_id = existing["id"]

            mod._schedule_reflection_prompt("bootup")

        all_files = [f for f in inbox.iterdir() if f.suffix == ".json"]
        ids = [json.loads(f.read_text()).get("id") for f in all_files]
        assert ids.count(existing_id) == 1, (
            f"expected exactly 1 file with id={existing_id!r}, got ids={ids}"
        )

    def test_reflection_already_exists_returns_true_for_id_in_inbox(self, tmp_path):
        """_reflection_already_exists returns True when the ID exists in inbox/."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        processed = tmp_path / "processed"
        processed.mkdir()

        msg_id = "reflection_bootup_9999999"
        (inbox / "9999999_reflection_bootup.json").write_text(
            json.dumps({"id": msg_id, "type": "reflection_prompt"})
        )

        mod = _load_on_fresh_start(
            inbox_dir=str(inbox),
            processing_dir=str(processing),
            processed_dir=str(processed),
        )

        assert mod._reflection_already_exists(msg_id) is True

    def test_reflection_already_exists_returns_false_for_absent_id(self, tmp_path):
        """_reflection_already_exists returns False when no file has that ID."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        processed = tmp_path / "processed"
        processed.mkdir()

        mod = _load_on_fresh_start(
            inbox_dir=str(inbox),
            processing_dir=str(processing),
            processed_dir=str(processed),
        )

        assert mod._reflection_already_exists("reflection_bootup_totally_absent") is False


# ---------------------------------------------------------------------------
# Helpers for time patching
# ---------------------------------------------------------------------------

class _PatchTime:
    """Context manager that patches time.time() to return a fixed integer.

    Usage::

        with _PatchTime(1700000000):
            result = module_under_test._schedule_reflection_prompt("compaction")
    """

    def __init__(self, fixed_ts: int | float):
        self._fixed_ts = float(fixed_ts)
        self._orig = None

    def __enter__(self):
        import time as _t
        self._orig = _t.time
        _t.time = lambda: self._fixed_ts
        return self

    def __exit__(self, *_):
        import time as _t
        _t.time = self._orig
