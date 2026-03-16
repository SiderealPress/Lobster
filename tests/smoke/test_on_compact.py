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
) -> types.ModuleType:
    """
    Load hooks/on-compact.py as a fresh module with patched path constants.

    We reload from source each time so tests are fully isolated — no shared
    module-level state leaks between test runs.
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

    # Suppress the Telegram dev-notify side-effect — we never want real network
    # calls in smoke tests and we don't want to depend on config.env being present.
    mod.maybe_send_dev_telegram_notify = lambda: None  # type: ignore[attr-defined]

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
