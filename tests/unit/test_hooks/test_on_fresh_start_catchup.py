"""
Unit tests for the stale-catchup compact-reminder injection in
hooks/on-fresh-start.py (issue #909 safety net), and the stale-claim cleanup
added for issue #1398.

Validates:
- _is_catchup_stale() correctly identifies stale / fresh / missing state
- _compact_reminder_already_queued() correctly detects existing reminders
- _inject_compact_reminder() writes the correct message or skips when one exists
- _clear_stale_claim() deletes stale message_claims rows before re-injection
"""

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "on-fresh-start.py"


class _PatchEnv:
    """Context manager to temporarily set environment variables."""

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


def _load_on_fresh_start(
    compaction_state_override: str = None,
    inbox_dir: str = None,
    session_file_pointer_override: str = None,
):
    """Load on-fresh-start.py as a module, overriding file paths for isolation."""
    # We need to patch the module-level constants after load, so we patch env vars
    # that are read at import time and then re-patch constants after import.
    env_patch = {}
    if compaction_state_override:
        env_patch["LOBSTER_COMPACTION_STATE_FILE_OVERRIDE"] = compaction_state_override
    if session_file_pointer_override:
        env_patch["LOBSTER_CURRENT_SESSION_FILE_OVERRIDE"] = session_file_pointer_override

    with _PatchEnv(env_patch):
        spec = importlib.util.spec_from_file_location("on_fresh_start", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        # Avoid session_role import failing — stub it
        sys.modules.setdefault("session_role", _make_session_role_stub())
        spec.loader.exec_module(mod)

    # Override runtime-resolved paths on the loaded module
    if compaction_state_override:
        mod.COMPACTION_STATE_FILE = Path(compaction_state_override)
    if inbox_dir:
        mod.INBOX_DIR = Path(inbox_dir)
    if session_file_pointer_override:
        mod.CURRENT_SESSION_FILE_POINTER = Path(session_file_pointer_override)
    return mod


def _make_session_role_stub():
    """Return a minimal session_role stub module."""
    import types
    stub = types.ModuleType("session_role")
    stub.is_dispatcher = lambda data: True
    return stub


class TestIsCatchupStale:
    """Tests for _is_catchup_stale()."""

    def test_returns_true_when_file_absent(self, tmp_path):
        mod = _load_on_fresh_start(
            compaction_state_override=str(tmp_path / "nonexistent.json"),
        )
        assert mod._is_catchup_stale() is True

    def test_returns_true_when_last_catchup_ts_absent(self, tmp_path):
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_compaction_ts": "2026-01-01T00:00:00Z"}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is True

    def test_returns_true_when_catchup_is_old(self, tmp_path):
        # Timestamp 2 hours ago is definitely stale
        two_hours_ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 7200),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": two_hours_ago}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is True

    def test_returns_false_when_catchup_is_recent(self, tmp_path):
        # Timestamp 1 minute ago — well within 30-min threshold
        one_min_ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 60),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": one_min_ago}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is False

    def test_threshold_boundary_just_over(self, tmp_path):
        # 31 minutes ago — should be stale
        threshold = 31 * 60
        old_ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - threshold),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": old_ts}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is True

    def test_threshold_boundary_just_under(self, tmp_path):
        # 29 minutes ago — should not be stale
        threshold = 29 * 60
        recent_ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - threshold),
        )
        state_file = tmp_path / "compaction-state.json"
        state_file.write_text(json.dumps({"last_catchup_ts": recent_ts}))
        mod = _load_on_fresh_start(compaction_state_override=str(state_file))
        assert mod._is_catchup_stale() is False



class TestHasRecentSessionFile:
    """Tests for _has_recent_session_file()."""

    def test_returns_false_when_pointer_absent(self, tmp_path):
        """No pointer file → no recent session, return False."""
        pointer = tmp_path / "nonexistent-pointer"
        mod = _load_on_fresh_start(session_file_pointer_override=str(pointer))
        assert mod._has_recent_session_file() is False

    def test_returns_false_when_pointer_empty(self, tmp_path):
        """Empty pointer file → no session path, return False."""
        pointer = tmp_path / "pointer"
        pointer.write_text("")
        mod = _load_on_fresh_start(session_file_pointer_override=str(pointer))
        assert mod._has_recent_session_file() is False

    def test_returns_false_when_session_file_absent(self, tmp_path):
        """Pointer points to nonexistent session file → return False."""
        pointer = tmp_path / "pointer"
        pointer.write_text(str(tmp_path / "nonexistent-session.md"))
        mod = _load_on_fresh_start(session_file_pointer_override=str(pointer))
        assert mod._has_recent_session_file() is False

    def test_returns_true_when_session_file_recent(self, tmp_path):
        """Session file modified 5 min ago (within 4h) → return True."""
        session_file = tmp_path / "20260331-001.md"
        session_file.write_text("# Session 20260331-001")
        pointer = tmp_path / "pointer"
        pointer.write_text(str(session_file))
        mod = _load_on_fresh_start(session_file_pointer_override=str(pointer))
        assert mod._has_recent_session_file() is True

    def test_returns_false_when_session_file_old(self, tmp_path):
        """Session file modified > 4h ago → return False."""
        import os as _os
        session_file = tmp_path / "20260330-003.md"
        session_file.write_text("# Session 20260330-003")
        # Set mtime to 5 hours ago
        five_hours_ago = time.time() - (5 * 60 * 60)
        _os.utime(str(session_file), (five_hours_ago, five_hours_ago))
        pointer = tmp_path / "pointer"
        pointer.write_text(str(session_file))
        mod = _load_on_fresh_start(session_file_pointer_override=str(pointer))
        assert mod._has_recent_session_file() is False

    def test_boundary_just_within_4h(self, tmp_path):
        """Session file modified 3h59m ago → return True."""
        import os as _os
        session_file = tmp_path / "session.md"
        session_file.write_text("# Session")
        just_within = time.time() - (4 * 60 * 60 - 60)  # 3h59m ago
        _os.utime(str(session_file), (just_within, just_within))
        pointer = tmp_path / "pointer"
        pointer.write_text(str(session_file))
        mod = _load_on_fresh_start(session_file_pointer_override=str(pointer))
        assert mod._has_recent_session_file() is True

    def test_boundary_just_over_4h(self, tmp_path):
        """Session file modified 4h1m ago → return False."""
        import os as _os
        session_file = tmp_path / "session.md"
        session_file.write_text("# Session")
        just_over = time.time() - (4 * 60 * 60 + 60)  # 4h1m ago
        _os.utime(str(session_file), (just_over, just_over))
        pointer = tmp_path / "pointer"
        pointer.write_text(str(session_file))
        mod = _load_on_fresh_start(session_file_pointer_override=str(pointer))
        assert mod._has_recent_session_file() is False

    def test_handles_oserror_gracefully(self, tmp_path):
        """OSError reading pointer → return False without raising."""
        mod = _load_on_fresh_start()
        # Point to /proc path that exists but read fails with PermissionError
        mod.CURRENT_SESSION_FILE_POINTER = Path("/proc/1/mem")
        # Must not raise
        result = mod._has_recent_session_file()
        assert result is False



class TestCompactReminderAlreadyQueued:
    """Tests for _compact_reminder_already_queued()."""

    def test_returns_false_when_inbox_empty(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        assert mod._compact_reminder_already_queued() is False

    def test_returns_false_when_inbox_absent(self, tmp_path):
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = tmp_path / "nonexistent_inbox"
        assert mod._compact_reminder_already_queued() is False

    def test_returns_true_when_compact_reminder_exists(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        msg = {"id": "0_compact", "subtype": "compact-reminder", "text": "test"}
        (inbox / "0_compact.json").write_text(json.dumps(msg))

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        assert mod._compact_reminder_already_queued() is True

    def test_returns_false_when_only_regular_messages(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        msg = {"id": "123_msg", "subtype": "user_message", "text": "hi"}
        (inbox / "123_msg.json").write_text(json.dumps(msg))

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        assert mod._compact_reminder_already_queued() is False

    def test_ignores_malformed_json(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "broken.json").write_text("{not valid json")

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        # Should not raise, and should return False (no valid compact-reminder found)
        assert mod._compact_reminder_already_queued() is False

    def test_returns_true_when_reminder_in_processing(self, tmp_path):
        """Reminder claimed by dispatcher (in processing/) counts as already queued.

        Regression test: before the fix, only inbox/ was checked. A reminder in
        processing/ was invisible, causing a duplicate to be injected on startup.
        """
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        msg = {"id": "0_compact", "subtype": "compact-reminder", "text": "test"}
        (processing / "0_compact.json").write_text(json.dumps(msg))

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        mod.PROCESSING_DIR = processing
        assert mod._compact_reminder_already_queued() is True

    def test_returns_false_when_processing_absent(self, tmp_path):
        """When processing/ does not exist, should not raise and return False."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        mod.PROCESSING_DIR = tmp_path / "nonexistent_processing"
        assert mod._compact_reminder_already_queued() is False


class TestInjectCompactReminder:
    """Tests for _inject_compact_reminder()."""

    def test_writes_compact_reminder_to_inbox(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        files = list(inbox.iterdir())
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["subtype"] == "compact-reminder"
        assert data["source"] == "system"
        assert data["chat_id"] == 0

    def test_injected_message_id_is_0_startup_compact(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].name == "0_startup_compact.json"

    def test_sorts_before_real_messages(self, tmp_path):
        """0_startup_compact.json must sort before epoch-ms user message filenames."""
        startup_name = "0_startup_compact.json"
        real_msg_name = "1773695000000_msg.json"
        # Lexicographic sort: "0_..." < epoch-ms names
        sorted_names = sorted([real_msg_name, startup_name])
        assert sorted_names[0] == startup_name

    def test_skips_when_reminder_already_queued(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        # Pre-populate with an existing compact-reminder
        existing = {"id": "0_compact", "subtype": "compact-reminder", "text": "existing"}
        (inbox / "0_compact.json").write_text(json.dumps(existing))

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        # Should not have added a second file
        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].name == "0_compact.json"

    def test_creates_inbox_dir_if_absent(self, tmp_path):
        inbox = tmp_path / "inbox_not_yet_created"
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        assert inbox.exists()
        files = list(inbox.iterdir())
        assert len(files) == 1

    def test_silent_on_permission_error(self, tmp_path):
        """_inject_compact_reminder must not raise on write failure."""
        # Point to a path that can't be created
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = Path("/proc/lobster_test_nonexistent/inbox")
        # Must not raise
        mod._inject_compact_reminder()

    def test_injected_text_contains_compact_reminder_instructions(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox

        mod._inject_compact_reminder()

        files = list(inbox.iterdir())
        data = json.loads(files[0].read_text())
        text = data["text"]
        assert "compact_catchup" in text or "compact-catchup" in text or "catchup" in text.lower()
        assert "wait_for_messages" in text


def _make_claims_db(path: Path) -> None:
    """Create a minimal agent_sessions.db with the message_claims table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_claims (
            message_id  TEXT PRIMARY KEY,
            claimed_by  TEXT NOT NULL,
            claimed_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'processing'
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_claim(db_path: Path, message_id: str, status: str = "processed") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO message_claims (message_id, claimed_by, claimed_at, status) VALUES (?, ?, ?, ?)",
        (message_id, "test-session", "2026-04-02T00:00:00+00:00", status),
    )
    conn.commit()
    conn.close()


def _row_exists(db_path: Path, message_id: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT 1 FROM message_claims WHERE message_id=?", (message_id,)
    ).fetchone()
    conn.close()
    return row is not None


class TestClearStaleClaim:
    """Tests for _clear_stale_claim() — issue #1398."""

    def test_deletes_processed_row(self, tmp_path):
        db = tmp_path / "config" / "agent_sessions.db"
        _make_claims_db(db)
        _insert_claim(db, "0_startup_compact", status="processed")

        mod = _load_on_fresh_start()
        mod.AGENT_SESSIONS_DB = db

        mod._clear_stale_claim("0_startup_compact")

        assert not _row_exists(db, "0_startup_compact")

    def test_deletes_processing_row(self, tmp_path):
        """Also clears rows with status='processing' (stuck mid-session)."""
        db = tmp_path / "config" / "agent_sessions.db"
        _make_claims_db(db)
        _insert_claim(db, "0_startup_compact", status="processing")

        mod = _load_on_fresh_start()
        mod.AGENT_SESSIONS_DB = db

        mod._clear_stale_claim("0_startup_compact")

        assert not _row_exists(db, "0_startup_compact")

    def test_noop_when_row_absent(self, tmp_path):
        """No-op when the row does not exist — must not raise."""
        db = tmp_path / "config" / "agent_sessions.db"
        _make_claims_db(db)

        mod = _load_on_fresh_start()
        mod.AGENT_SESSIONS_DB = db

        # Should not raise
        mod._clear_stale_claim("0_startup_compact")

    def test_noop_when_db_absent(self, tmp_path):
        """No-op when agent_sessions.db does not exist — must not raise."""
        mod = _load_on_fresh_start()
        mod.AGENT_SESSIONS_DB = tmp_path / "config" / "agent_sessions.db"

        # Must not raise
        mod._clear_stale_claim("0_startup_compact")

    def test_does_not_delete_other_rows(self, tmp_path):
        """Only the target message_id row is deleted; unrelated rows are untouched."""
        db = tmp_path / "config" / "agent_sessions.db"
        _make_claims_db(db)
        _insert_claim(db, "0_startup_compact", status="processed")
        _insert_claim(db, "some_other_message", status="processed")

        mod = _load_on_fresh_start()
        mod.AGENT_SESSIONS_DB = db

        mod._clear_stale_claim("0_startup_compact")

        assert not _row_exists(db, "0_startup_compact")
        assert _row_exists(db, "some_other_message")

    def test_inject_compact_reminder_clears_claim_before_writing(self, tmp_path):
        """End-to-end: inject succeeds even when a stale claim row exists.

        This is the regression test for issue #1398: mark_processing would
        return already_claimed because the message_claims row from a previous
        session persisted after restart.  The fix is that _inject_compact_reminder
        calls _clear_stale_claim before writing the file, ensuring the new
        dispatcher can claim the message cleanly.
        """
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        db = tmp_path / "config" / "agent_sessions.db"
        _make_claims_db(db)
        # Simulate a stale row from the previous dispatcher session
        _insert_claim(db, "0_startup_compact", status="processed")

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        mod.AGENT_SESSIONS_DB = db

        mod._inject_compact_reminder()

        # File must have been written
        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].name == "0_startup_compact.json"

        # Stale claim row must have been cleared
        assert not _row_exists(db, "0_startup_compact")

    def test_inject_compact_reminder_removes_stale_processing_file(self, tmp_path):
        """End-to-end: stale processing/ file is removed before re-injection.

        A crashed or compacted dispatcher session may leave the message file in
        processing/ instead of inbox/.  Without cleanup the new dispatcher's
        mark_processing would fail because the file already exists at the
        destination path.  The fix removes any stale processing/ file before
        writing a fresh copy to inbox/.
        """
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        processing = tmp_path / "processing"
        processing.mkdir()
        db = tmp_path / "config" / "agent_sessions.db"
        _make_claims_db(db)

        # Simulate a stale processing file from the previous dispatcher session
        stale_file = processing / "0_startup_compact.json"
        stale_file.write_text('{"id": "0_startup_compact"}\n')

        mod = _load_on_fresh_start()
        mod.INBOX_DIR = inbox
        mod.PROCESSING_DIR = processing
        mod.AGENT_SESSIONS_DB = db

        mod._inject_compact_reminder()

        # Fresh file must be in inbox/
        inbox_files = list(inbox.iterdir())
        assert len(inbox_files) == 1
        assert inbox_files[0].name == "0_startup_compact.json"

        # Stale processing file must have been removed
        assert not stale_file.exists()
