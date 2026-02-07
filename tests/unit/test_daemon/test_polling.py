"""
Tests for Daemon Polling Behavior

Tests polling intervals and loop behavior.
"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import time


class TestPollingIntervals:
    """Tests for polling interval behavior."""

    def test_poll_interval_constant(self):
        """Test that POLL_INTERVAL is 5 seconds."""
        from src.daemon.daemon import POLL_INTERVAL
        assert POLL_INTERVAL == 5

    def test_idle_poll_interval_constant(self):
        """Test that IDLE_POLL_INTERVAL is 10 seconds."""
        from src.daemon.daemon import IDLE_POLL_INTERVAL
        assert IDLE_POLL_INTERVAL == 10

    def test_claude_timeout_constant(self):
        """Test that CLAUDE_TIMEOUT is 300 seconds."""
        from src.daemon.daemon import CLAUDE_TIMEOUT
        assert CLAUDE_TIMEOUT == 300


class TestProcessMessages:
    """Tests for process_messages function."""

    @pytest.fixture
    def workspace(self, temp_workspace: Path) -> Path:
        """Get workspace directory."""
        return temp_workspace

    @pytest.mark.asyncio
    async def test_uses_session_id_for_new_session(self, workspace: Path):
        """Test that --session-id is used for new session."""
        session_file = workspace / ".lobster_session_id"
        session_used = workspace / ".lobster_session_used"

        # Ensure no session exists
        if session_file.exists():
            session_file.unlink()
        if session_used.exists():
            session_used.unlink()

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = MagicMock()
                    mock_proc.communicate = AsyncMock(
                        return_value=(b"Success", b"")
                    )
                    mock_proc.returncode = 0
                    mock_exec.return_value = mock_proc

                    from src.daemon.daemon import process_messages

                    success, output = await process_messages()

                    # Check that --session-id was used (not --resume)
                    call_args = mock_exec.call_args[0]
                    assert "--session-id" in call_args
                    assert "--resume" not in call_args

    @pytest.mark.asyncio
    async def test_uses_resume_for_existing_session(self, workspace: Path):
        """Test that --resume is used for existing session."""
        session_file = workspace / ".lobster_session_id"
        session_used = workspace / ".lobster_session_used"

        # Create existing session
        session_file.write_text("existing-session-id")
        session_used.write_text(str(time.time()))

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = MagicMock()
                    mock_proc.communicate = AsyncMock(
                        return_value=(b"Success", b"")
                    )
                    mock_proc.returncode = 0
                    mock_exec.return_value = mock_proc

                    from src.daemon.daemon import process_messages

                    success, output = await process_messages()

                    # Check that --resume was used
                    call_args = mock_exec.call_args[0]
                    assert "--resume" in call_args
                    assert "existing-session-id" in call_args

    @pytest.mark.asyncio
    async def test_marks_session_used_after_success(self, workspace: Path):
        """Test that session is marked as used after successful run."""
        session_file = workspace / ".lobster_session_id"
        session_used = workspace / ".lobster_session_used"

        # Fresh session
        if session_used.exists():
            session_used.unlink()

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = MagicMock()
                    mock_proc.communicate = AsyncMock(
                        return_value=(b"Success", b"")
                    )
                    mock_proc.returncode = 0
                    mock_exec.return_value = mock_proc

                    from src.daemon.daemon import process_messages

                    await process_messages()

                    assert session_used.exists()

    @pytest.mark.asyncio
    async def test_returns_false_on_error(self, workspace: Path):
        """Test that False is returned on Claude error."""
        session_file = workspace / ".lobster_session_id"

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = MagicMock()
                    mock_proc.communicate = AsyncMock(
                        return_value=(b"", b"Error occurred")
                    )
                    mock_proc.returncode = 1
                    mock_exec.return_value = mock_proc

                    from src.daemon.daemon import process_messages

                    success, output = await process_messages()

                    assert success is False
                    assert "Error occurred" in output

    @pytest.mark.asyncio
    async def test_handles_timeout(self, workspace: Path):
        """Test that timeout is handled."""
        session_file = workspace / ".lobster_session_id"

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
                with patch("src.daemon.daemon.CLAUDE_TIMEOUT", 0.1):
                    with patch("asyncio.create_subprocess_exec") as mock_exec:
                        mock_proc = MagicMock()

                        async def slow_communicate():
                            await asyncio.sleep(10)
                            return (b"", b"")

                        mock_proc.communicate = slow_communicate
                        mock_proc.kill = MagicMock()
                        mock_exec.return_value = mock_proc

                        from src.daemon.daemon import process_messages

                        success, output = await process_messages()

                        assert success is False
                        assert "Timeout" in output

    @pytest.mark.asyncio
    async def test_includes_dangerously_skip_permissions(self, workspace: Path):
        """Test that --dangerously-skip-permissions is included."""
        session_file = workspace / ".lobster_session_id"

        with patch("src.daemon.daemon.WORKSPACE", workspace):
            with patch("src.daemon.daemon.SESSION_ID_FILE", session_file):
                with patch("asyncio.create_subprocess_exec") as mock_exec:
                    mock_proc = MagicMock()
                    mock_proc.communicate = AsyncMock(
                        return_value=(b"Success", b"")
                    )
                    mock_proc.returncode = 0
                    mock_exec.return_value = mock_proc

                    from src.daemon.daemon import process_messages

                    await process_messages()

                    call_args = mock_exec.call_args[0]
                    assert "--dangerously-skip-permissions" in call_args
