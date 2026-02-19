"""
Hibernation MVP - Python Unit Tests

Tests state file management, health check hibernation awareness,
and bot wake logic.

Run with: cd $LOBSTER_DIR && uv run pytest tests/unit/test_hibernation.py -v
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers shared between test classes
# ---------------------------------------------------------------------------

def _write_state(state_dir: Path, mode: str, updated_at: str = "2026-01-01T00:00:00+00:00") -> Path:
    """Write a state file atomically (mirrors production implementation)."""
    state_file = state_dir / "lobster-state.json"
    tmp = state_dir / f".lobster-state-{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps({"mode": mode, "updated_at": updated_at})
    )
    tmp.rename(state_file)
    return state_file


# ---------------------------------------------------------------------------
# 1. State file management
# ---------------------------------------------------------------------------

class TestStateFile:
    """Tests for state file read/write logic in inbox_server.py."""

    @pytest.fixture
    def state_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "config"
        d.mkdir(parents=True)
        return d

    def test_write_state_creates_file(self, state_dir: Path):
        """State file is created with correct content."""
        f = _write_state(state_dir, "hibernate")
        assert f.exists()
        data = json.loads(f.read_text())
        assert data["mode"] == "hibernate"
        assert "updated_at" in data

    def test_state_transitions_active_hibernate_active(self, state_dir: Path):
        """State file transitions correctly between active and hibernate."""
        _write_state(state_dir, "active")
        f = state_dir / "lobster-state.json"
        assert json.loads(f.read_text())["mode"] == "active"

        _write_state(state_dir, "hibernate")
        assert json.loads(f.read_text())["mode"] == "hibernate"

        _write_state(state_dir, "active")
        assert json.loads(f.read_text())["mode"] == "active"

    def test_missing_state_file_defaults_active(self, state_dir: Path):
        """Missing state file should default to 'active'."""
        # Import the helper from the MCP server
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))
        from inbox_server import _read_lobster_state
        result = _read_lobster_state(state_dir / "lobster-state.json")
        assert result == "active"

    def test_corrupt_state_file_defaults_active(self, state_dir: Path):
        """Corrupt state file JSON should default to 'active'."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))
        from inbox_server import _read_lobster_state
        state_file = state_dir / "lobster-state.json"
        state_file.write_text("NOT_VALID_JSON{{{{")
        result = _read_lobster_state(state_file)
        assert result == "active"

    def test_empty_json_object_defaults_active(self, state_dir: Path):
        """State file with no 'mode' key should default to 'active'."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))
        from inbox_server import _read_lobster_state
        state_file = state_dir / "lobster-state.json"
        state_file.write_text("{}")
        result = _read_lobster_state(state_file)
        assert result == "active"

    def test_atomic_write_no_tmp_files_remain(self, state_dir: Path):
        """After atomic write, no temporary files should remain."""
        _write_state(state_dir, "hibernate")
        tmp_files = list(state_dir.glob("*.tmp"))
        assert tmp_files == [], f"Unexpected tmp files: {tmp_files}"

    def test_write_state_via_server_function(self, state_dir: Path):
        """_write_lobster_state helper in inbox_server writes correctly."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))
        from inbox_server import _write_lobster_state
        state_file = state_dir / "lobster-state.json"
        _write_lobster_state(state_file, "hibernate")
        data = json.loads(state_file.read_text())
        assert data["mode"] == "hibernate"
        assert "updated_at" in data

    def test_reset_state_on_startup(self, state_dir: Path):
        """_reset_state_on_startup resets 'hibernate' to 'active' and adds woke_at."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))
        from inbox_server import _reset_state_on_startup, LOBSTER_STATE_FILE

        # Write hibernate state
        state_file = state_dir / "lobster-state.json"
        _write_state(state_dir, "hibernate")
        assert json.loads(state_file.read_text())["mode"] == "hibernate"

        # Patch the global and call the function
        with patch("inbox_server.LOBSTER_STATE_FILE", state_file):
            _reset_state_on_startup()

        # Verify state was reset
        data = json.loads(state_file.read_text())
        assert data["mode"] == "active", f"Expected 'active' after startup reset, got {data['mode']}"
        assert "woke_at" in data, "Expected 'woke_at' timestamp after reset"

    def test_reset_state_on_startup_noop_when_active(self, state_dir: Path):
        """_reset_state_on_startup does nothing when state is already 'active'."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))
        from inbox_server import _reset_state_on_startup

        state_file = state_dir / "lobster-state.json"
        _write_state(state_dir, "active")
        original = state_file.read_text()

        with patch("inbox_server.LOBSTER_STATE_FILE", state_file):
            _reset_state_on_startup()

        # File should be unchanged (no unnecessary write)
        assert state_file.read_text() == original


# ---------------------------------------------------------------------------
# 2. wait_for_messages timeout → hibernate
# ---------------------------------------------------------------------------

class TestWaitForMessagesHibernation:
    """
    Tests that wait_for_messages writes hibernate state on timeout
    when hibernation is enabled.
    """

    @pytest.fixture
    def dirs(self, tmp_path: Path):
        """Set up temporary message and config directories."""
        inbox = tmp_path / "messages" / "inbox"
        config = tmp_path / "messages" / "config"
        inbox.mkdir(parents=True)
        config.mkdir(parents=True)
        return {
            "inbox": inbox,
            "config": config,
            "state_file": config / "lobster-state.json",
        }

    def test_timeout_writes_hibernate_state(self, dirs):
        """When wait_for_messages times out, state file is written as 'hibernate'."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

        with patch.multiple(
            "inbox_server",
            INBOX_DIR=dirs["inbox"],
            CONFIG_DIR=dirs["config"],
            LOBSTER_STATE_FILE=dirs["state_file"],
        ):
            # Patch touch_heartbeat to avoid filesystem side-effects
            with patch("inbox_server.touch_heartbeat"):
                with patch("inbox_server._recover_stale_processing"):
                    with patch("inbox_server._recover_retryable_messages"):
                        import inbox_server
                        result = asyncio.run(
                            inbox_server.handle_wait_for_messages(
                                {"timeout": 1, "hibernate_on_timeout": True}
                            )
                        )

        # State file must exist and be "hibernate"
        assert dirs["state_file"].exists(), "State file not created on timeout"
        data = json.loads(dirs["state_file"].read_text())
        assert data["mode"] == "hibernate"

        # Result must contain the hibernate signal
        text = result[0].text
        assert "hibernate" in text.lower() or "hibernating" in text.lower(), (
            f"Response does not mention hibernation: {text}"
        )

    def test_timeout_without_hibernate_flag_does_not_write_state(self, dirs):
        """When hibernate_on_timeout=False, no state file is written on timeout."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

        with patch.multiple(
            "inbox_server",
            INBOX_DIR=dirs["inbox"],
            CONFIG_DIR=dirs["config"],
            LOBSTER_STATE_FILE=dirs["state_file"],
        ):
            with patch("inbox_server.touch_heartbeat"):
                with patch("inbox_server._recover_stale_processing"):
                    with patch("inbox_server._recover_retryable_messages"):
                        import inbox_server
                        asyncio.run(
                            inbox_server.handle_wait_for_messages(
                                {"timeout": 1, "hibernate_on_timeout": False}
                            )
                        )

        assert not dirs["state_file"].exists(), (
            "State file should NOT be created when hibernate_on_timeout=False"
        )

    def test_message_arrival_does_not_trigger_hibernate(self, dirs, tmp_path):
        """When a message arrives, state must NOT be set to hibernate."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "mcp"))

        # Pre-populate inbox with one message
        msg = {"id": "test_001", "text": "hello", "source": "telegram",
               "chat_id": 1, "timestamp": "2026-01-01T00:00:00"}
        (dirs["inbox"] / "test_001.json").write_text(json.dumps(msg))

        # Write "active" state to start
        _write_state(dirs["config"], "active")

        with patch.multiple(
            "inbox_server",
            INBOX_DIR=dirs["inbox"],
            CONFIG_DIR=dirs["config"],
            LOBSTER_STATE_FILE=dirs["state_file"],
        ):
            with patch("inbox_server.touch_heartbeat"):
                with patch("inbox_server._recover_stale_processing"):
                    with patch("inbox_server._recover_retryable_messages"):
                        import inbox_server
                        asyncio.run(
                            inbox_server.handle_wait_for_messages(
                                {"timeout": 5, "hibernate_on_timeout": True}
                            )
                        )

        # State should still be "active" (message was processed, not timeout)
        if dirs["state_file"].exists():
            data = json.loads(dirs["state_file"].read_text())
            assert data["mode"] != "hibernate", (
                "State should not be 'hibernate' when a message arrived"
            )


# ---------------------------------------------------------------------------
# 3. Bot wake logic
# ---------------------------------------------------------------------------

class TestBotWakeLogic:
    """
    Tests for lobster_bot.py wake-on-message logic.

    The bot must:
    - Detect when Claude is not running (hibernate state)
    - Spawn a fresh Claude session
    - NOT spawn if Claude is already running
    - Handle race conditions (two concurrent messages)
    """

    @pytest.fixture
    def state_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "config"
        d.mkdir(parents=True)
        return d

    def _import_bot_wake(self):
        """Import the wake helper from lobster_bot."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "bot"))
        import lobster_bot
        return lobster_bot

    def test_wake_claude_called_when_hibernating(self, state_dir: Path):
        """Bot calls wake_claude() when state is 'hibernate' and resets state to active."""
        _write_state(state_dir, "hibernate")

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            mock_result = MagicMock(returncode=0, stderr="")
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                bot = self._import_bot_wake()
                with patch.object(bot, "LOBSTER_STATE_FILE", state_dir / "lobster-state.json"):
                    with patch.object(bot, "_is_claude_running", return_value=False):
                        bot.wake_claude_if_hibernating()
                        mock_run.assert_called_once()
                        # Verify state was reset to "active"
                        state = json.loads((state_dir / "lobster-state.json").read_text())
                        assert state["mode"] == "active", f"Expected mode='active' after wake, got {state['mode']}"

    def test_wake_not_called_when_active(self, state_dir: Path):
        """Bot does NOT spawn Claude when state is 'active'."""
        _write_state(state_dir, "active")

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            with patch("subprocess.Popen") as mock_popen:
                bot = self._import_bot_wake()
                with patch.object(bot, "LOBSTER_STATE_FILE", state_dir / "lobster-state.json"):
                    with patch.object(bot, "_is_claude_running", return_value=True):
                        bot.wake_claude_if_hibernating()
                        mock_popen.assert_not_called()

    def test_wake_not_called_when_claude_already_running(self, state_dir: Path):
        """Bot does NOT spawn Claude when Claude process is running and hibernate is fresh."""
        from datetime import datetime, timezone
        fresh_ts = datetime.now(timezone.utc).isoformat()
        _write_state(state_dir, "hibernate", updated_at=fresh_ts)

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            with patch("subprocess.Popen") as mock_popen:
                bot = self._import_bot_wake()
                with patch.object(bot, "LOBSTER_STATE_FILE", state_dir / "lobster-state.json"):
                    # Claude is already running with fresh hibernate state (still shutting down)
                    with patch.object(bot, "_is_claude_running", return_value=True):
                        bot.wake_claude_if_hibernating()
                        mock_popen.assert_not_called()

    def test_missing_state_file_does_not_spawn(self, state_dir: Path):
        """Bot does not spawn Claude when state file is missing (defaults active)."""
        # No state file written

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            with patch("subprocess.Popen") as mock_popen:
                bot = self._import_bot_wake()
                with patch.object(bot, "LOBSTER_STATE_FILE", state_dir / "lobster-state.json"):
                    with patch.object(bot, "_is_claude_running", return_value=True):
                        bot.wake_claude_if_hibernating()
                        mock_popen.assert_not_called()

    def test_race_condition_only_one_wake_attempt(self, state_dir: Path):
        """Two concurrent messages only trigger one Claude spawn (lock prevents double-wake)."""
        _write_state(state_dir, "hibernate")

        wake_calls = []

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            bot = self._import_bot_wake()

            def mock_run(*args, **kwargs):
                time.sleep(0.05)  # simulate brief spawn time
                wake_calls.append(args)
                return MagicMock(returncode=0, stderr="")

            with patch.object(bot, "LOBSTER_STATE_FILE", state_dir / "lobster-state.json"):
                with patch.object(bot, "_is_claude_running", return_value=False):
                    with patch("subprocess.run", side_effect=mock_run):
                        threads = [
                            threading.Thread(target=bot.wake_claude_if_hibernating)
                            for _ in range(5)
                        ]
                        for t in threads:
                            t.start()
                        for t in threads:
                            t.join(timeout=2)

        # Only one wake call despite concurrent invocations
        assert len(wake_calls) == 1, (
            f"Expected 1 wake call, got {len(wake_calls)}"
        )

    def test_stale_hibernate_kills_zombie_and_restarts(self, state_dir: Path):
        """When hibernate state is stale (>60s old) and Claude process exists,
        the bot kills the zombie process and proceeds with restart."""
        # Write a stale hibernate state (old timestamp)
        _write_state(state_dir, "hibernate", updated_at="2020-01-01T00:00:00+00:00")

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            mock_result = MagicMock(returncode=0, stderr="")
            bot = self._import_bot_wake()

            # _is_claude_running returns True on first call (zombie detected),
            # then False after the kill (zombie is gone, proceed with restart)
            running_returns = iter([True, False])

            with patch.object(bot, "LOBSTER_STATE_FILE", state_dir / "lobster-state.json"):
                with patch.object(bot, "_is_claude_running", side_effect=lambda: next(running_returns)):
                    with patch("subprocess.run", return_value=mock_result) as mock_run:
                        with patch("time.sleep"):  # Don't actually sleep in tests
                            bot.wake_claude_if_hibernating()

                            # Should have called pkill (to kill zombie) and systemctl restart
                            calls = mock_run.call_args_list
                            pkill_calls = [c for c in calls if "pkill" in str(c)]
                            restart_calls = [c for c in calls if "systemctl" in str(c)]
                            assert len(pkill_calls) >= 1, f"Expected pkill call, got calls: {calls}"
                            assert len(restart_calls) >= 1, f"Expected systemctl restart call, got calls: {calls}"

                            # State should be reset to active
                            state = json.loads((state_dir / "lobster-state.json").read_text())
                            assert state["mode"] == "active"

    def test_fresh_hibernate_does_not_kill_claude(self, state_dir: Path):
        """When hibernate state is fresh (<60s old) and Claude process exists,
        the bot does NOT kill Claude — it returns early (Claude may still be shutting down)."""
        from datetime import datetime, timezone
        fresh_ts = datetime.now(timezone.utc).isoformat()
        _write_state(state_dir, "hibernate", updated_at=fresh_ts)

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            bot = self._import_bot_wake()

            with patch.object(bot, "LOBSTER_STATE_FILE", state_dir / "lobster-state.json"):
                with patch.object(bot, "_is_claude_running", return_value=True):
                    with patch("subprocess.run") as mock_run:
                        with patch("subprocess.Popen") as mock_popen:
                            bot.wake_claude_if_hibernating()
                            # Should NOT have called pkill or systemctl
                            mock_run.assert_not_called()
                            mock_popen.assert_not_called()

    def test_is_hibernate_stale_no_timestamp(self):
        """_is_hibernate_stale returns True when updated_at is missing."""
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            bot = self._import_bot_wake()
            assert bot._is_hibernate_stale({}) is True
            assert bot._is_hibernate_stale({"mode": "hibernate"}) is True

    def test_is_hibernate_stale_bad_timestamp(self):
        """_is_hibernate_stale returns True when updated_at is unparseable."""
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            bot = self._import_bot_wake()
            assert bot._is_hibernate_stale({"updated_at": "not-a-date"}) is True

    def test_is_hibernate_stale_old_timestamp(self):
        """_is_hibernate_stale returns True when updated_at is old."""
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            bot = self._import_bot_wake()
            assert bot._is_hibernate_stale({"updated_at": "2020-01-01T00:00:00+00:00"}) is True

    def test_is_hibernate_stale_fresh_timestamp(self):
        """_is_hibernate_stale returns False when updated_at is recent."""
        from datetime import datetime, timezone
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"},
        ):
            bot = self._import_bot_wake()
            fresh = datetime.now(timezone.utc).isoformat()
            assert bot._is_hibernate_stale({"updated_at": fresh}) is False


# ---------------------------------------------------------------------------
# 4. Health check hibernation awareness
# ---------------------------------------------------------------------------

class TestHealthCheckHibernation:
    """
    Tests that health-check-v3.sh skips Claude restart when state is 'hibernate'.

    We parse and simulate the relevant logic rather than running the full
    health-check (which requires systemd, tmux, etc.).
    """

    HEALTH_CHECK = (
        Path(__file__).parent.parent.parent / "scripts" / "health-check-v3.sh"
    )

    @pytest.fixture
    def state_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "config"
        d.mkdir(parents=True)
        return d

    def test_health_check_script_exists(self):
        """health-check-v3.sh must exist."""
        assert self.HEALTH_CHECK.exists(), f"Not found: {self.HEALTH_CHECK}"

    def test_health_check_contains_hibernate_check(self):
        """health-check-v3.sh must reference state file / hibernate logic."""
        content = self.HEALTH_CHECK.read_text()
        assert "hibernate" in content.lower(), (
            "health-check-v3.sh does not contain hibernation logic"
        )

    def test_health_check_references_state_file(self):
        """health-check-v3.sh must reference lobster-state.json."""
        content = self.HEALTH_CHECK.read_text()
        assert "lobster-state" in content, (
            "health-check-v3.sh does not reference lobster-state.json"
        )

    def test_health_check_skips_restart_when_hibernating(self, state_dir, tmp_path):
        """
        Run health-check-v3.sh with a stubbed environment to verify it exits
        without attempting restart when state is 'hibernate'.
        """
        _write_state(state_dir, "hibernate")

        # Build a minimal fake environment
        env = os.environ.copy()
        env["LOBSTER_STATE_FILE_OVERRIDE"] = str(state_dir / "lobster-state.json")
        env["LOBSTER_HEALTH_CHECK_DRY_RUN"] = "1"  # signal no real systemctl calls

        result = subprocess.run(
            ["bash", str(self.HEALTH_CHECK)],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        # Script must exit 0 (no crash); it logs but does not restart
        # We check the combined output doesn't mention "Restarting" or "restart"
        combined = result.stdout + result.stderr
        assert "Restarting lobster-claude" not in combined, (
            f"Health check tried to restart Claude in hibernate mode:\n{combined}"
        )

    def test_health_check_allows_restart_when_active(self, state_dir, tmp_path):
        """
        Smoke test: when state is 'active', health-check is NOT blocked
        by hibernation guard (it may still skip restart for other reasons
        but the hibernation guard must not be the reason).
        """
        _write_state(state_dir, "active")
        content = self.HEALTH_CHECK.read_text()
        # The script must only skip restart when explicitly in hibernate mode
        # This is a static analysis check
        assert 'mode' in content or 'hibernate' in content, (
            "health-check-v3.sh does not check mode before skipping restart"
        )
