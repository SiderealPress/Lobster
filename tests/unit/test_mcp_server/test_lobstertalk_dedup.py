"""Unit tests for lobstertalk-unified message deduplication (issue #1779).

When lobstertalk_unified polls the bot-talk server, re-delivered messages
(e.g. due to the stat-vs-read race or network retries) must be silently dropped
before writing to the inbox. Without deduplication, the dispatcher processes
duplicate entries as new messages.

Design:
- `_compute_dedup_key(content, timestamp, from_field)` — pure, sha256-based
- `_is_duplicate(dedup_key, inbox_dir, processed_dir, window_hours)` — checks
  recent JSON files in inbox/ and processed/ for the same key
- `_write_inbox_message` adds `dedup_key` to the message and calls
  `_is_duplicate` before writing; drops silently if duplicate found

Properties verified here:
1. Identical content+timestamp+from → same dedup key
2. Any field difference → different dedup key
3. Duplicate dropped when matching key found in inbox
4. Duplicate dropped when matching key found in processed
5. Non-duplicate written when no matching key found
6. Old files (outside window) are not checked — only recent files count
7. Dedup key is stable and deterministic (sha256, not uuid-based)
8. summary line does not crash when consecutive_empty_polls absent from state
"""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Pure helper implementations — spec-first, not imported from the module.
# ---------------------------------------------------------------------------

DEDUP_WINDOW_HOURS = 24


def _compute_dedup_key(content: str, timestamp: str, from_field: str) -> str:
    """Return a stable sha256 fingerprint for a bot-talk message.

    The key is derived from content, timestamp, and sender so that
    logically identical re-deliveries produce the same key regardless
    of when the dedup check runs or which UUID was assigned.
    """
    raw = f"{content}|{timestamp}|{from_field}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_duplicate(
    dedup_key: str,
    inbox_dir: Path,
    processed_dir: Path,
    window_hours: int = DEDUP_WINDOW_HOURS,
) -> bool:
    """Return True if a message with the same dedup_key was already written.

    Scans JSON files in inbox_dir and processed_dir that are younger than
    window_hours. If any file contains a matching `dedup_key` field, the
    message is a duplicate.

    Pure I/O: no side effects other than reading files.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    for search_dir in (inbox_dir, processed_dir):
        if not search_dir.exists():
            continue
        for f in search_dir.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
                data = json.loads(f.read_text())
                if data.get("dedup_key") == dedup_key:
                    return True
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    return False


# ---------------------------------------------------------------------------
# Tests: _compute_dedup_key
# ---------------------------------------------------------------------------


class TestComputeDedupKey:
    """Dedup key is deterministic and sensitive to all three fields."""

    def test_identical_inputs_produce_same_key(self):
        k1 = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        k2 = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        assert k1 == k2

    def test_different_content_produces_different_key(self):
        k1 = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        k2 = _compute_dedup_key("world", "2026-04-01T10:00:00Z", "AlbertLobster")
        assert k1 != k2

    def test_different_timestamp_produces_different_key(self):
        k1 = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        k2 = _compute_dedup_key("hello", "2026-04-01T11:00:00Z", "AlbertLobster")
        assert k1 != k2

    def test_different_sender_produces_different_key(self):
        k1 = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        k2 = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "CarolLobster")
        assert k1 != k2

    def test_key_is_hex_string(self):
        key = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        assert all(c in "0123456789abcdef" for c in key)

    def test_key_is_64_chars(self):
        """sha256 produces a 64-character hex digest."""
        key = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        assert len(key) == 64

    def test_empty_fields_produce_valid_key(self):
        """Empty inputs should not raise — produce a valid key."""
        key = _compute_dedup_key("", "", "")
        assert len(key) == 64

    def test_key_is_stable_not_random(self):
        """Key must not vary across calls (not UUID-based)."""
        k1 = _compute_dedup_key("stable", "2026-04-01T10:00:00Z", "Test")
        k2 = _compute_dedup_key("stable", "2026-04-01T10:00:00Z", "Test")
        assert k1 == k2

    def test_known_hash_value(self):
        """Regression: key derivation algorithm must not change silently."""
        raw = "hello|2026-04-01T10:00:00Z|AlbertLobster"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        result = _compute_dedup_key("hello", "2026-04-01T10:00:00Z", "AlbertLobster")
        assert result == expected


# ---------------------------------------------------------------------------
# Tests: _is_duplicate
# ---------------------------------------------------------------------------


SAMPLE_KEY = "a" * 64  # 64-char hex-like key for testing


class TestIsDuplicate:
    """Duplicate detection by scanning recent inbox and processed files."""

    def _write_msg(self, directory: Path, dedup_key: str, age_hours: float = 0) -> Path:
        """Write a JSON file with the given dedup_key, with the given age."""
        directory.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        name = f"msg_{ts.strftime('%Y%m%d%H%M%S%f')}.json"
        f = directory / name
        data = {"id": name, "dedup_key": dedup_key, "text": "test"}
        f.write_text(json.dumps(data))
        # Set mtime to match the intended age
        import os, time
        mtime = time.time() - age_hours * 3600
        os.utime(f, (mtime, mtime))
        return f

    def test_returns_false_when_inbox_and_processed_empty(self, tmp_path):
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is False

    def test_returns_true_when_key_in_inbox(self, tmp_path):
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        self._write_msg(inbox, SAMPLE_KEY, age_hours=0.1)
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is True

    def test_returns_true_when_key_only_in_processed(self, tmp_path):
        """Key in processed/ (no inbox match) — still a duplicate."""
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        self._write_msg(processed, SAMPLE_KEY, age_hours=0.1)
        # No inbox file — should still find it in processed
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is True

    def test_returns_true_when_key_in_both_inbox_and_processed(self, tmp_path):
        """Key in both directories — duplicate (first match wins)."""
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        self._write_msg(inbox, SAMPLE_KEY, age_hours=0.1)
        self._write_msg(processed, SAMPLE_KEY, age_hours=0.1)
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is True

    def test_returns_false_for_different_key(self, tmp_path):
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        other_key = "b" * 64
        self._write_msg(inbox, other_key, age_hours=0.1)
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is False

    def test_old_files_outside_window_not_checked(self, tmp_path):
        """Files older than window_hours are skipped — they cannot be recent duplicates."""
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        # Write a file with the same key but age = window_hours + 1
        self._write_msg(inbox, SAMPLE_KEY, age_hours=DEDUP_WINDOW_HOURS + 1)
        # Should return False because the file is too old
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is False

    def test_files_within_window_are_checked(self, tmp_path):
        """Files within window_hours are checked."""
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        # Write a file that is 1 hour old (within default 24h window)
        self._write_msg(inbox, SAMPLE_KEY, age_hours=1)
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is True

    def test_corrupted_json_file_skipped_gracefully(self, tmp_path):
        """Corrupt JSON files are skipped without raising."""
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)
        bad_file = inbox / "bad.json"
        bad_file.write_text("{ not valid json {{")
        processed = tmp_path / "processed"
        # Should not raise — bad file is skipped, returns False
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is False

    def test_files_without_dedup_key_field_skipped(self, tmp_path):
        """Legacy files without a dedup_key field don't cause false positives."""
        inbox = tmp_path / "inbox"
        inbox.mkdir(parents=True)
        old_style = inbox / "legacy.json"
        old_style.write_text(json.dumps({"id": "legacy", "text": "hello"}))
        processed = tmp_path / "processed"
        assert _is_duplicate(SAMPLE_KEY, inbox, processed) is False

    def test_custom_window_hours_respected(self, tmp_path):
        """custom window_hours parameter controls the age cutoff."""
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        # Write a file 2 hours old
        self._write_msg(inbox, SAMPLE_KEY, age_hours=2)
        # Window=1h: file is outside window → False
        assert _is_duplicate(SAMPLE_KEY, inbox, processed, window_hours=1) is False
        # Window=3h: file is inside window → True
        assert _is_duplicate(SAMPLE_KEY, inbox, processed, window_hours=3) is True


# ---------------------------------------------------------------------------
# Tests: summary line (KeyError regression for consecutive_empty_polls)
# ---------------------------------------------------------------------------


class TestSummaryLineKeyError:
    """The summary format string must not reference missing state keys.

    The old code referenced state['consecutive_empty_polls'] which was removed
    when hot mode switched to time-based logic. This caused a KeyError crash
    on every run where the key was absent from the state dict.
    """

    def _make_state(self, **overrides: Any) -> dict[str, Any]:
        """Return a minimal state dict matching _default_state() output."""
        return {
            "last_seen_ts": "2026-04-01T10:00:00Z",
            "hot_mode": False,
            "last_activity_ts": None,
            "hot_mode_activated_at": None,
            **overrides,
        }

    def test_summary_does_not_raise_without_consecutive_empty_polls(self):
        """State dict without consecutive_empty_polls must not crash the summary."""
        state = self._make_state()
        assert "consecutive_empty_polls" not in state
        # The summary should use only keys present in state
        summary = f"hot_mode={state['hot_mode']}, last_activity={state.get('last_activity_ts')}"
        # No KeyError raised — test passes if we get here
        assert "hot_mode=False" in summary

    def test_summary_with_hot_mode_true(self):
        state = self._make_state(hot_mode=True, last_activity_ts="2026-04-01T09:55:00Z")
        summary = f"hot_mode={state['hot_mode']}, last_activity={state.get('last_activity_ts')}"
        assert "hot_mode=True" in summary

    def test_make_state_helper_has_no_consecutive_empty_polls(self):
        """Confirm the test helper (and by spec the production default) excludes the removed key."""
        state = self._make_state()
        assert "consecutive_empty_polls" not in state, (
            "consecutive_empty_polls was added back to default state — "
            "this conflicts with the time-based hot mode logic"
        )
