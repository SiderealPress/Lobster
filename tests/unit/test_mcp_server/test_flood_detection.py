"""
Unit tests for inbox flood detection (issue #1420).

The flood detector auto-drains reconciler startup ghosts before the dispatcher
sees the inbox. A ghost is a subagent_result with:
  - elapsed_seconds < GHOST_ELAPSED_THRESHOLD_SECONDS (30)
  - message timestamp within STARTUP_WINDOW_SECONDS (60) of server start

Tests verify:
  1. _is_reconciler_ghost() correctly classifies ghost vs non-ghost messages
  2. _drain_reconciler_ghosts() moves ghosts to processed/ and leaves real messages
  3. Named constants match the spec (30s elapsed threshold, 60s startup window)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[3]

for _p in [
    str(_ROOT / "src" / "mcp"),
    str(_ROOT / "src" / "agents"),
    str(_ROOT / "src"),
    str(_ROOT / "src" / "utils"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Fixture: load inbox_server module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def inbox_server_module(tmp_path_factory):
    """Load inbox_server with a fresh temporary messages directory."""
    import os

    tmp = tmp_path_factory.mktemp("flood_base")
    os.environ.setdefault("LOBSTER_MESSAGES", str(tmp / "messages"))
    os.environ.setdefault("LOBSTER_WORKSPACE", str(tmp / "workspace"))

    try:
        if "inbox_server" in sys.modules:
            del sys.modules["inbox_server"]
        import inbox_server as _is
        return _is
    except Exception:
        pytest.skip("inbox_server not importable in this test environment")


@pytest.fixture
def is_ghost(inbox_server_module):
    """Return the _is_reconciler_ghost pure function."""
    return inbox_server_module._is_reconciler_ghost


@pytest.fixture
def constants(inbox_server_module):
    """Return the flood detection named constants."""
    return {
        "elapsed_threshold": inbox_server_module.GHOST_ELAPSED_THRESHOLD_SECONDS,
        "startup_window": inbox_server_module.STARTUP_WINDOW_SECONDS,
    }


# ---------------------------------------------------------------------------
# Shared message factories
# ---------------------------------------------------------------------------

SERVER_START = datetime(2026, 4, 13, 10, 0, 0, tzinfo=timezone.utc)
WITHIN_WINDOW = SERVER_START + timedelta(seconds=30)  # 30s after start — within 60s window
OUTSIDE_WINDOW = SERVER_START + timedelta(seconds=90)  # 90s after start — outside window


def _make_subagent_result(elapsed_seconds: int, timestamp: datetime) -> dict:
    """Return a subagent_result inbox message with the given fields."""
    return {
        "id": f"test-{timestamp.timestamp():.0f}-e{elapsed_seconds}",
        "type": "subagent_result",
        "source": "telegram",
        "chat_id": "8305714125",
        "elapsed_seconds": elapsed_seconds,
        "task_id": "some-task",
        "status": "success",
        "timestamp": timestamp.isoformat(),
    }


def _make_text_message(timestamp: datetime) -> dict:
    """Return a user text message (never a ghost)."""
    return {
        "id": f"user-{timestamp.timestamp():.0f}",
        "type": "text",
        "source": "telegram",
        "chat_id": "8305714125",
        "text": "Hello lobster",
        "timestamp": timestamp.isoformat(),
    }


def _make_agent_failed(elapsed_seconds: int, timestamp: datetime) -> dict:
    """Return an agent_failed message (never a ghost by type)."""
    return {
        "id": f"failed-{timestamp.timestamp():.0f}",
        "type": "agent_failed",
        "source": "system",
        "chat_id": 0,
        "elapsed_seconds": elapsed_seconds,
        "timestamp": timestamp.isoformat(),
    }


# ---------------------------------------------------------------------------
# Tests: named constants match the spec
# ---------------------------------------------------------------------------

class TestFloodDetectionConstants:
    """Named constants must match the values specified in issue #1420."""

    SPEC_GHOST_ELAPSED_THRESHOLD = 30   # "elapsed < 30 seconds"
    SPEC_STARTUP_WINDOW = 60            # "arrived within 60 seconds of dispatcher startup"

    def test_ghost_elapsed_threshold_matches_spec(self, constants):
        """GHOST_ELAPSED_THRESHOLD_SECONDS must be 30 per issue #1420 spec."""
        assert constants["elapsed_threshold"] == self.SPEC_GHOST_ELAPSED_THRESHOLD, (
            f"Spec requires 30s elapsed threshold, got {constants['elapsed_threshold']}"
        )

    def test_startup_window_matches_spec(self, constants):
        """STARTUP_WINDOW_SECONDS must be 60 per issue #1420 spec."""
        assert constants["startup_window"] == self.SPEC_STARTUP_WINDOW, (
            f"Spec requires 60s startup window, got {constants['startup_window']}"
        )


# ---------------------------------------------------------------------------
# Tests: _is_reconciler_ghost — pure classification function
# ---------------------------------------------------------------------------

class TestIsReconcilerGhost:
    """_is_reconciler_ghost() correctly classifies messages."""

    def test_short_elapsed_within_window_is_ghost(self, is_ghost):
        """subagent_result with elapsed<30s within 60s of server start is a ghost."""
        msg = _make_subagent_result(elapsed_seconds=0, timestamp=WITHIN_WINDOW)
        assert is_ghost(msg, SERVER_START) is True

    def test_29s_elapsed_within_window_is_ghost(self, is_ghost):
        """elapsed_seconds=29 (one below threshold) within window is a ghost."""
        msg = _make_subagent_result(elapsed_seconds=29, timestamp=WITHIN_WINDOW)
        assert is_ghost(msg, SERVER_START) is True

    def test_30s_elapsed_within_window_not_ghost(self, is_ghost):
        """elapsed_seconds=30 (at threshold) is NOT a ghost — real work was done."""
        msg = _make_subagent_result(elapsed_seconds=30, timestamp=WITHIN_WINDOW)
        assert is_ghost(msg, SERVER_START) is False

    def test_long_elapsed_within_window_not_ghost(self, is_ghost):
        """elapsed_seconds=120 (real task) within window is NOT a ghost."""
        msg = _make_subagent_result(elapsed_seconds=120, timestamp=WITHIN_WINDOW)
        assert is_ghost(msg, SERVER_START) is False

    def test_short_elapsed_outside_window_not_ghost(self, is_ghost):
        """elapsed_seconds=5 but arrived >60s after server start — NOT a ghost.

        A short-elapsed message that arrives long after startup is a legitimately
        fast task, not a startup sweep artifact.
        """
        msg = _make_subagent_result(elapsed_seconds=5, timestamp=OUTSIDE_WINDOW)
        assert is_ghost(msg, SERVER_START) is False

    def test_wrong_type_not_ghost(self, is_ghost):
        """Only subagent_result type can be a ghost — other types are never ghosts."""
        msg = _make_agent_failed(elapsed_seconds=0, timestamp=WITHIN_WINDOW)
        assert is_ghost(msg, SERVER_START) is False

    def test_user_text_message_not_ghost(self, is_ghost):
        """User text messages are never ghosts."""
        msg = _make_text_message(WITHIN_WINDOW)
        assert is_ghost(msg, SERVER_START) is False

    def test_missing_timestamp_not_ghost(self, is_ghost):
        """Messages without timestamp field are not classified as ghosts (safe fallback)."""
        msg = _make_subagent_result(elapsed_seconds=0, timestamp=WITHIN_WINDOW)
        del msg["timestamp"]
        assert is_ghost(msg, SERVER_START) is False

    def test_missing_elapsed_seconds_defaults_to_zero(self, is_ghost):
        """Missing elapsed_seconds defaults to 0 — still a ghost if within window."""
        msg = _make_subagent_result(elapsed_seconds=0, timestamp=WITHIN_WINDOW)
        del msg["elapsed_seconds"]
        assert is_ghost(msg, SERVER_START) is True

    def test_none_elapsed_seconds_defaults_to_zero(self, is_ghost):
        """elapsed_seconds=None defaults to 0 — still a ghost if within window."""
        msg = _make_subagent_result(elapsed_seconds=0, timestamp=WITHIN_WINDOW)
        msg["elapsed_seconds"] = None
        assert is_ghost(msg, SERVER_START) is True

    def test_at_startup_window_boundary_is_ghost(self, is_ghost):
        """Message at exactly STARTUP_WINDOW_SECONDS after start is still a ghost."""
        exactly_at_boundary = SERVER_START + timedelta(seconds=60)
        msg = _make_subagent_result(elapsed_seconds=0, timestamp=exactly_at_boundary)
        assert is_ghost(msg, SERVER_START) is True

    def test_one_second_past_window_not_ghost(self, is_ghost):
        """Message at STARTUP_WINDOW_SECONDS + 1 second is NOT a ghost."""
        just_past = SERVER_START + timedelta(seconds=61)
        msg = _make_subagent_result(elapsed_seconds=0, timestamp=just_past)
        assert is_ghost(msg, SERVER_START) is False

    def test_before_server_start_not_ghost(self, is_ghost):
        """Message timestamped before server start (negative age) is not a ghost."""
        before_start = SERVER_START - timedelta(seconds=5)
        msg = _make_subagent_result(elapsed_seconds=0, timestamp=before_start)
        assert is_ghost(msg, SERVER_START) is False


# ---------------------------------------------------------------------------
# Tests: _drain_reconciler_ghosts — I/O function
# ---------------------------------------------------------------------------

class TestDrainReconcilerGhosts:
    """_drain_reconciler_ghosts() moves ghosts to processed/ and leaves real messages."""

    @pytest.fixture
    def drain_env(self, tmp_path, inbox_server_module, monkeypatch):
        """Set up isolated inbox/processed directories and patch inbox_server globals."""
        inbox_dir = tmp_path / "inbox"
        processed_dir = tmp_path / "processed"
        inbox_dir.mkdir()
        processed_dir.mkdir()

        monkeypatch.setattr(inbox_server_module, "INBOX_DIR", inbox_dir)
        monkeypatch.setattr(inbox_server_module, "PROCESSED_DIR", processed_dir)
        monkeypatch.setattr(inbox_server_module, "_SERVER_START_TIME", SERVER_START)

        return {
            "inbox": inbox_dir,
            "processed": processed_dir,
            "drain": inbox_server_module._drain_reconciler_ghosts,
        }

    def _write_message(self, directory: Path, msg: dict) -> Path:
        """Write a message dict to a JSON file in directory."""
        path = directory / f"{msg['id']}.json"
        path.write_text(json.dumps(msg))
        return path

    def test_ghost_is_moved_to_processed(self, drain_env):
        """A single reconciler ghost is moved from inbox/ to processed/."""
        ghost = _make_subagent_result(elapsed_seconds=0, timestamp=WITHIN_WINDOW)
        ghost_file = self._write_message(drain_env["inbox"], ghost)

        count = drain_env["drain"]()

        assert count == 1
        assert not ghost_file.exists(), "Ghost should have been moved out of inbox"
        assert (drain_env["processed"] / ghost_file.name).exists(), (
            "Ghost should be in processed/"
        )

    def test_real_message_is_not_drained(self, drain_env):
        """A real subagent_result (long elapsed) is not touched by drain."""
        real = _make_subagent_result(elapsed_seconds=120, timestamp=WITHIN_WINDOW)
        real_file = self._write_message(drain_env["inbox"], real)

        count = drain_env["drain"]()

        assert count == 0
        assert real_file.exists(), "Real message must stay in inbox"

    def test_user_message_is_not_drained(self, drain_env):
        """User text messages are never touched by drain."""
        user_msg = _make_text_message(WITHIN_WINDOW)
        user_file = self._write_message(drain_env["inbox"], user_msg)

        count = drain_env["drain"]()

        assert count == 0
        assert user_file.exists(), "User message must stay in inbox"

    def test_multiple_ghosts_all_drained(self, drain_env):
        """Multiple ghosts in a single startup sweep are all drained."""
        ghosts = [
            _make_subagent_result(elapsed_seconds=i, timestamp=WITHIN_WINDOW)
            for i in range(5)
        ]
        for g in ghosts:
            self._write_message(drain_env["inbox"], g)

        count = drain_env["drain"]()

        assert count == 5
        assert list(drain_env["inbox"].glob("*.json")) == [], (
            "All ghosts should be drained from inbox"
        )
        assert len(list(drain_env["processed"].glob("*.json"))) == 5

    def test_mixed_inbox_drains_only_ghosts(self, drain_env):
        """When inbox has both ghosts and real messages, only ghosts are drained."""
        ghost = _make_subagent_result(elapsed_seconds=5, timestamp=WITHIN_WINDOW)
        real_result = _make_subagent_result(elapsed_seconds=180, timestamp=WITHIN_WINDOW)
        user_msg = _make_text_message(WITHIN_WINDOW)

        ghost_file = self._write_message(drain_env["inbox"], ghost)
        real_file = self._write_message(drain_env["inbox"], real_result)
        user_file = self._write_message(drain_env["inbox"], user_msg)

        count = drain_env["drain"]()

        assert count == 1
        assert not ghost_file.exists()
        assert real_file.exists()
        assert user_file.exists()

    def test_empty_inbox_returns_zero(self, drain_env):
        """Empty inbox drain returns 0 with no errors."""
        count = drain_env["drain"]()
        assert count == 0

    def test_outside_window_ghosts_not_drained(self, drain_env):
        """Short-elapsed messages outside the startup window are not drained."""
        late_msg = _make_subagent_result(elapsed_seconds=0, timestamp=OUTSIDE_WINDOW)
        late_file = self._write_message(drain_env["inbox"], late_msg)

        count = drain_env["drain"]()

        assert count == 0
        assert late_file.exists(), "Message outside startup window must not be drained"
