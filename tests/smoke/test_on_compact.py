"""
Smoke tests – Group A: hooks/on-compact.py

These tests verify the context-compaction hook works correctly.

Why these tests exist:
- A1: A syntax error in on-compact.py silently breaks all compaction flow.
  The hook is invoked by Claude Code's SessionStart event; if it crashes,
  the dispatcher never gets its re-orientation reminder after a context wipe.
- A2: If the compact-reminder message is not written to the inbox, the
  dispatcher will resume processing user messages with a blank context,
  not knowing it was just compacted. This is a silent correctness failure.
- A3: The health check reads compacted_at from lobster-state.json to suppress
  false-positive "stale inbox" restarts during the compaction pause window.
  If this field is missing or unparseable the health check will restart the
  dispatcher unnecessarily.
- A4: PR #237 fixed double-compaction. If the hook is run twice without an
  intervening wait_for_messages() call, there should still be exactly one
  compact-reminder in the inbox, not two. This is the regression test.
- A5: send_compaction_notify() must be called on every compaction, regardless
  of LOBSTER_DEBUG.  Previously the notification was gated on LOBSTER_DEBUG,
  causing 0 notifications in production and 2+ when health-check also alerted.
  This test pins the always-on behaviour.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest


@contextmanager
def _stdin_from_module(mod: types.ModuleType):
    """Replace sys.stdin with a StringIO containing the module's test hook input."""
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(mod._test_stdin_data)
    try:
        yield
    finally:
        sys.stdin = old_stdin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOK_PATH = Path(__file__).parents[2] / "hooks" / "on-compact.py"


def _load_hook(
    inbox_dir: Path,
    state_file: Path,
    sentinel_file: Path,
    session_id: str = "test-dispatcher-session",
    processing_dir: Path | None = None,
) -> types.ModuleType:
    """
    Load hooks/on-compact.py as a fresh module with patched path constants.

    We reload from source each time so tests are fully isolated — no shared
    module-level state leaks between test runs.

    processing_dir: optional override for PROCESSING_DIR; defaults to a
    non-existent tmp subdirectory so existing tests are unaffected.
    """
    spec = importlib.util.spec_from_file_location("on_compact", HOOK_PATH)
    assert spec is not None, f"Could not load spec from {HOOK_PATH}"
    assert spec.loader is not None

    mod = importlib.util.module_from_spec(spec)
    # Execute the module so all top-level code runs (defines functions, etc.)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]

    # Patch the module-level path constants AFTER loading so they take effect
    # for all subsequent function calls within the module.
    mod.INBOX_DIR = inbox_dir
    mod.STATE_FILE = state_file
    mod.SENTINEL_FILE = sentinel_file
    # Default: a directory that doesn't exist so it's safely skipped.
    mod.PROCESSING_DIR = processing_dir if processing_dir is not None else (inbox_dir.parent / "processing")

    # Suppress the Telegram notify side-effect — we never want real network
    # calls in smoke tests and we don't want to depend on config.env being present.
    mod.send_compaction_notify = lambda: None  # type: ignore[attr-defined]

    # Stub is_dispatcher to return True so tests exercise the dispatcher path.
    # The real is_dispatcher() checks a marker file + transcript; in tests there
    # is no marker file and no stdin transcript, so it would return False and
    # silently exit without writing any inbox/state files.
    mod.is_dispatcher = lambda _data: True  # type: ignore[attr-defined]

    # Patch sys.stdin so main()'s json.load(sys.stdin) gets a valid compact event
    # rather than hitting pytest's captured stdin which raises OSError.
    hook_input = json.dumps(
        {"session_id": session_id, "hook_event_name": "SessionStart", "is_compact": True}
    )
    mod._test_stdin_data = hook_input  # store for reference

    return mod


def _compact_reminders(inbox_dir: Path) -> list[Path]:
    """Return all JSON files in inbox_dir that have subtype=compact-reminder."""
    return [
        p
        for p in inbox_dir.glob("*.json")
        if json.loads(p.read_text()).get("subtype") == "compact-reminder"
    ]


# ---------------------------------------------------------------------------
# A1 – runs without crashing
# ---------------------------------------------------------------------------


def test_on_compact_runs_without_crashing(tmp_path: Path) -> None:
    """
    A1: on-compact.py must exit cleanly (no exception) when invoked.

    Failure mode: a syntax error, import error, or unhandled exception in
    the hook silently breaks all compaction flow. Claude Code reports a hook
    error but does NOT abort the session, so the dispatcher continues running
    with a blank context and no re-orientation reminder.
    """
    inbox_dir = tmp_path / "inbox"
    state_file = tmp_path / "config" / "lobster-state.json"
    sentinel_file = tmp_path / "config" / "compact-pending"

    mod = _load_hook(inbox_dir, state_file, sentinel_file)

    # Should complete without raising.
    with _stdin_from_module(mod):
        mod.main()


# ---------------------------------------------------------------------------
# A2 – writes a compact-reminder to the inbox
# ---------------------------------------------------------------------------


def test_on_compact_writes_compact_reminder(tmp_path: Path) -> None:
    """
    A2: After running, a file with subtype="compact-reminder" and
    source="system" must exist in the inbox directory.

    Failure mode: if write_reminder() is skipped or the file has wrong fields,
    wait_for_messages() will never surface the re-orientation prompt and the
    dispatcher proceeds with a blank context after compaction.
    """
    inbox_dir = tmp_path / "inbox"
    state_file = tmp_path / "config" / "lobster-state.json"
    sentinel_file = tmp_path / "config" / "compact-pending"

    mod = _load_hook(inbox_dir, state_file, sentinel_file)
    with _stdin_from_module(mod):
        mod.main()

    reminders = _compact_reminders(inbox_dir)
    assert len(reminders) == 1, (
        f"Expected exactly 1 compact-reminder in inbox, found {len(reminders)}"
    )

    data = json.loads(reminders[0].read_text())
    assert data.get("subtype") == "compact-reminder", (
        f"subtype field wrong: {data.get('subtype')!r}"
    )
    assert data.get("source") == "system", (
        f"source field wrong: {data.get('source')!r}"
    )


# ---------------------------------------------------------------------------
# A3 – writes a parseable ISO timestamp to lobster-state.json
# ---------------------------------------------------------------------------


def test_on_compact_writes_compacted_at_to_state(tmp_path: Path) -> None:
    """
    A3: After running, lobster-state.json must contain a parseable ISO-8601
    timestamp in the "compacted_at" field.

    Failure mode: the health check reads compacted_at to decide whether a
    stale inbox is expected (because the dispatcher is paused waiting for the
    compact-reminder to surface). If the field is absent or malformed, the
    health check treats a silent post-compaction pause as a stuck dispatcher
    and issues an unnecessary restart.
    """
    inbox_dir = tmp_path / "inbox"
    state_file = tmp_path / "config" / "lobster-state.json"
    sentinel_file = tmp_path / "config" / "compact-pending"

    mod = _load_hook(inbox_dir, state_file, sentinel_file)
    with _stdin_from_module(mod):
        mod.main()

    assert state_file.exists(), f"lobster-state.json was not created at {state_file}"

    state = json.loads(state_file.read_text())
    assert "compacted_at" in state, (
        f"'compacted_at' key missing from lobster-state.json; keys present: {list(state.keys())}"
    )

    raw_ts = state["compacted_at"]
    # Must be a non-empty string parseable as an ISO-8601 datetime.
    assert isinstance(raw_ts, str) and raw_ts, (
        f"compacted_at is not a non-empty string: {raw_ts!r}"
    )
    # Normalise trailing 'Z' → '+00:00' for Python < 3.11 fromisoformat compat.
    normalised = raw_ts.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        pytest.fail(
            f"compacted_at value {raw_ts!r} is not a valid ISO-8601 timestamp: {exc}"
        )

    # Sanity-check: timestamp should be recent (within the last minute).
    now = datetime.now(tz=timezone.utc)
    delta_seconds = abs((now - parsed.replace(tzinfo=timezone.utc)).total_seconds())
    assert delta_seconds < 60, (
        f"compacted_at timestamp {raw_ts!r} is {delta_seconds:.0f}s from now — "
        "expected a freshly written value"
    )


# ---------------------------------------------------------------------------
# A4 – idempotent (regression test for PR #237 double-compaction)
# ---------------------------------------------------------------------------


def test_on_compact_idempotent(tmp_path: Path) -> None:
    """
    A4: Running the hook twice must result in exactly one compact-reminder in
    the inbox, not two. The sentinel file must still be present after both runs.

    Failure mode: PR #237 fixed a bug where double-compaction (two SessionStart
    compact events before wait_for_messages() consumed the first reminder)
    would write a second compact-reminder. The dispatcher would then process
    both, causing it to re-read CLAUDE.md twice and potentially interleave
    re-orientation with real user messages.

    This test pins that behaviour: the hook must be idempotent.
    """
    inbox_dir = tmp_path / "inbox"
    state_file = tmp_path / "config" / "lobster-state.json"
    sentinel_file = tmp_path / "config" / "compact-pending"

    # First invocation — normal path.
    mod = _load_hook(inbox_dir, state_file, sentinel_file)
    with _stdin_from_module(mod):
        mod.main()

    # Second invocation — simulates double-compaction (compact event fires
    # again before the dispatcher has consumed the first reminder).
    mod2 = _load_hook(inbox_dir, state_file, sentinel_file)
    with _stdin_from_module(mod2):
        mod2.main()

    reminders = _compact_reminders(inbox_dir)
    assert len(reminders) == 1, (
        f"Expected exactly 1 compact-reminder after two runs, found {len(reminders)}. "
        "Double-compaction regression (see PR #237)."
    )

    assert sentinel_file.exists(), (
        f"Sentinel file {sentinel_file} was not present after second run. "
        "The gate hook relies on this file to block tool calls during compaction."
    )


# ---------------------------------------------------------------------------
# A5 – send_compaction_notify() is always called (not gated on LOBSTER_DEBUG)
# ---------------------------------------------------------------------------


def test_on_compact_always_sends_notification(tmp_path: Path) -> None:
    """
    A5: send_compaction_notify() must be called unconditionally on every
    compaction invocation, regardless of the LOBSTER_DEBUG setting.

    Previously the notification was gated on LOBSTER_DEBUG=true, which meant:
    - Production (LOBSTER_DEBUG=false): 0 notifications from on-compact.py,
      then 1-2 from health-check = wrong total.
    - Debug mode: 1 from on-compact.py + 1-2 from health-check = duplicates.

    The fix: always call send_compaction_notify(), and have health-check
    suppress its own alerts during the compaction window.  This test pins
    the always-on behaviour by verifying the call happens even when
    LOBSTER_DEBUG is explicitly false.

    Failure mode: if the notification is re-gated on LOBSTER_DEBUG, production
    users will again receive 0 notifications from on-compact.py and will see
    confusing health-check alerts instead.
    """
    inbox_dir = tmp_path / "inbox"
    state_file = tmp_path / "config" / "lobster-state.json"
    sentinel_file = tmp_path / "config" / "compact-pending"

    mod = _load_hook(inbox_dir, state_file, sentinel_file)

    # Replace send_compaction_notify with a tracking stub instead of the
    # no-op lambda from _load_hook so we can assert it was called.
    call_count: list[int] = [0]

    def _track_notify() -> None:
        call_count[0] += 1

    mod.send_compaction_notify = _track_notify  # type: ignore[attr-defined]

    # Ensure LOBSTER_DEBUG is NOT set in the environment so we can verify the
    # call happens without relying on the debug flag.
    old_env = os.environ.pop("LOBSTER_DEBUG", None)
    try:
        with _stdin_from_module(mod):
            mod.main()
    finally:
        if old_env is not None:
            os.environ["LOBSTER_DEBUG"] = old_env

    assert call_count[0] == 1, (
        f"Expected send_compaction_notify() to be called exactly once, "
        f"but it was called {call_count[0]} time(s). "
        "The notification must fire unconditionally, not gated on LOBSTER_DEBUG."
    )


# ---------------------------------------------------------------------------
# A6 – idempotent when reminder is in processing/ (not inbox/)
# ---------------------------------------------------------------------------


def test_on_compact_idempotent_when_reminder_in_processing(tmp_path: Path) -> None:
    """
    A6: If a compact-reminder is currently in processing/ (claimed by the
    dispatcher via mark_processing), the hook must NOT write a second reminder
    to inbox/.

    Failure mode (bug fixed in this PR): already_pending() only checked
    inbox/, so a rapid second compaction while the dispatcher was handling the
    first compact-reminder would write a duplicate.  The dispatcher would then
    process two compact-reminders, triggering two catch-up subagents and a
    confusing double re-orientation.
    """
    inbox_dir = tmp_path / "inbox"
    processing_dir = tmp_path / "processing"
    state_file = tmp_path / "config" / "lobster-state.json"
    sentinel_file = tmp_path / "config" / "compact-pending"

    # Simulate the first compact-reminder already having been claimed by the
    # dispatcher (moved from inbox/ to processing/).
    processing_dir.mkdir(parents=True)
    existing_reminder = {
        "id": "0_compact",
        "source": "system",
        "type": "compact-reminder",
        "subtype": "compact-reminder",
        "text": "COMPACT REMINDER",
    }
    (processing_dir / "0_compact.json").write_text(
        json.dumps(existing_reminder, indent=2) + "\n"
    )

    # inbox/ is empty — the reminder has already been claimed.
    inbox_dir.mkdir(parents=True)

    mod = _load_hook(
        inbox_dir, state_file, sentinel_file, processing_dir=processing_dir
    )
    with _stdin_from_module(mod):
        mod.main()

    # inbox/ must still be empty (no new compact-reminder written).
    inbox_reminders = [
        p
        for p in inbox_dir.glob("*.json")
        if json.loads(p.read_text()).get("subtype") == "compact-reminder"
    ]
    assert len(inbox_reminders) == 0, (
        f"Expected 0 compact-reminders in inbox/ (reminder was in processing/), "
        f"but found {len(inbox_reminders)}. Double-compaction regression — "
        "already_pending() must check processing/ as well as inbox/."
    )
