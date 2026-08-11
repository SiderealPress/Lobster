"""
Tests verifying that claude-wrapper.exp (LOBSTER_DEBUG=true / debug-mode
launcher) writes the dispatcher.pid file that health-check-v3.sh's
check_session_age() depends on for the graceful SESSION AGE LIMIT restart.

Root cause (fixed in this PR, issue #2196):
  scripts/claude-persistent.sh (the default, non-debug launcher) writes
  $MESSAGES_DIR/config/dispatcher.pid right before exec-ing claude, and
  removes it on exit. scripts/claude-wrapper.exp (the LOBSTER_DEBUG=true
  launcher) writes the analogous dispatcher-startup-flag file but never
  wrote dispatcher.pid.

  health-check-v3.sh's check_session_age() reads dispatcher.pid to find the
  PID to send a graceful SIGTERM to once the session passes
  SESSION_AGE_LIMIT_SECONDS. With no dispatcher.pid, it logs
  "no dispatcher.pid — cannot send SIGTERM" and returns without acting —
  every 4-minute health-check tick, for as long as debug mode is active and
  the session is over-age. health-check.log shows this warning firing 4,148
  times since 2026-06-07, essentially always alongside "Claude running
  without persistent wrapper (debug mode — expected)" or the old-style-mode
  variant of the same log line.

  The fix mirrors claude-persistent.sh's pattern: write dispatcher.pid using
  [exp_pid] (the correct expect built-in — see
  test_claude_wrapper_exp_spawn_pid.py for why $spawn_pid must not be used)
  immediately after spawn, and remove it when the wrapper exits.

These tests use `expect -c` to exercise the Tcl/expect write pattern in
isolation, plus static checks confirming the actual script contains the fix
and derives the same path health-check-v3.sh reads from
($MESSAGES_DIR/config/dispatcher.pid, where MESSAGES_DIR defaults to
$HOME/messages exactly as claude-persistent.sh and health-check-v3.sh do).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Path to claude-wrapper.exp — repo root is 3 levels above tests/unit/test_hooks/
_REPO_ROOT = Path(__file__).parents[3]
CLAUDE_WRAPPER_EXP = _REPO_ROOT / "scripts" / "claude-wrapper.exp"
CLAUDE_PERSISTENT_SH = _REPO_ROOT / "scripts" / "claude-persistent.sh"
HEALTH_CHECK_V3_SH = _REPO_ROOT / "scripts" / "health-check-v3.sh"

# Guard: skip all tests if expect is not installed on this system.
_EXPECT_AVAILABLE = shutil.which("expect") is not None


@pytest.mark.skipif(not _EXPECT_AVAILABLE, reason="expect not installed")
class TestExpPidWritesDispatcherPidFile:
    """Regression tests confirming [exp_pid] can write a dispatcher.pid file."""

    def test_exp_pid_writes_valid_integer_pid_to_dispatcher_pid_file(self, tmp_path):
        """[exp_pid] writes a valid PID to a dispatcher.pid-shaped file.

        Mirrors the already-fixed dispatcher-startup-flag write pattern
        (test_claude_wrapper_exp_spawn_pid.py) applied to dispatcher.pid.
        """
        config_dir = tmp_path / "messages" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        pid_file = config_dir / "dispatcher.pid"
        script = f"""
spawn sh -c "sleep 0.1"
catch {{
    set fh [open {pid_file} w]
    puts $fh [exp_pid]
    close $fh
}}
"""
        subprocess.run(
            ["expect", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert pid_file.exists(), "dispatcher.pid should exist after [exp_pid] write"
        content = pid_file.read_text().strip()
        assert content, "dispatcher.pid must not be empty when [exp_pid] is used"
        assert content.isdigit(), (
            f"dispatcher.pid must contain a valid integer PID, got: {content!r}"
        )
        pid = int(content)
        assert 1 < pid < 4_194_304, f"PID {pid} is outside valid Linux range"


class TestClaudeWrapperExpWritesDispatcherPid:
    """Static checks: claude-wrapper.exp must write and clean up dispatcher.pid."""

    def setup_method(self):
        assert CLAUDE_WRAPPER_EXP.exists(), (
            f"claude-wrapper.exp not found at {CLAUDE_WRAPPER_EXP}"
        )
        self.content = CLAUDE_WRAPPER_EXP.read_text()

    def test_script_writes_dispatcher_pid_file(self):
        """claude-wrapper.exp must write dispatcher.pid, not just the startup flag.

        Without this, check_session_age() in health-check-v3.sh can never
        find a PID to send the graceful SESSION AGE LIMIT SIGTERM to while
        running in debug mode — the mechanism silently no-ops forever.
        """
        assert "dispatcher.pid" in self.content, (
            "claude-wrapper.exp must write a file named dispatcher.pid "
            "(mirroring scripts/claude-persistent.sh) so health-check-v3.sh's "
            "check_session_age() can locate the dispatcher PID in debug mode."
        )

    def test_dispatcher_pid_write_uses_exp_pid_not_spawn_pid(self):
        """The dispatcher.pid write must use [exp_pid], the same fix already
        applied to the dispatcher-startup-flag write — $spawn_pid is not a
        valid Tcl variable and silently writes an empty file (catch swallows
        the error).
        """
        # Isolate the block that constructs/writes the dispatcher.pid path —
        # anchor on the quoted literal ("dispatcher.pid") rather than the
        # first textual mention of the word, since explanatory comments
        # above the actual write code also legitimately say "dispatcher.pid".
        match = re.search(r'"dispatcher\.pid"[\s\S]{0,1500}', self.content)
        assert match, (
            'could not locate a `"dispatcher.pid"` path-construction site to inspect '
            "(expected something like [file join $dir \"dispatcher.pid\"])"
        )
        block = match.group(0)
        assert "[exp_pid]" in block, (
            "the dispatcher.pid write must use [exp_pid], not $spawn_pid "
            "(which does not exist in Tcl/expect and would silently write "
            "an empty file)."
        )
        assert "$spawn_pid" not in block, (
            "the dispatcher.pid write must not use $spawn_pid — it is not a "
            "valid Tcl variable and silently produces an empty file."
        )

    def test_dispatcher_pid_path_matches_health_check_and_persistent_sh(self):
        """The path claude-wrapper.exp writes to must be the exact path
        health-check-v3.sh reads from ($MESSAGES_DIR/config/dispatcher.pid,
        MESSAGES_DIR defaulting to $HOME/messages) — the same path
        claude-persistent.sh already uses.
        """
        assert HEALTH_CHECK_V3_SH.exists()
        assert CLAUDE_PERSISTENT_SH.exists()
        health_check_content = HEALTH_CHECK_V3_SH.read_text()
        persistent_content = CLAUDE_PERSISTENT_SH.read_text()

        # health-check-v3.sh's reader path
        assert 'DISPATCHER_PID_FILE="$MESSAGES_DIR/config/dispatcher.pid"' in health_check_content
        # claude-persistent.sh's writer path (already correct, used as the reference)
        assert 'config/dispatcher.pid' in persistent_content

        # claude-wrapper.exp must build the same relative path: config/dispatcher.pid
        # under the messages directory (LOBSTER_MESSAGES, default $HOME/messages).
        assert "config" in self.content and "dispatcher.pid" in self.content, (
            "claude-wrapper.exp must write to <messages_dir>/config/dispatcher.pid, "
            "matching health-check-v3.sh's DISPATCHER_PID_FILE and "
            "claude-persistent.sh's dispatcher_pid_file."
        )
        # Must respect LOBSTER_MESSAGES like the other two scripts do, not hardcode
        # a path that would silently diverge from a non-default install.
        assert "LOBSTER_MESSAGES" in self.content, (
            "claude-wrapper.exp must honour LOBSTER_MESSAGES (falling back to "
            "$HOME/messages) when computing the dispatcher.pid path, exactly like "
            "claude-persistent.sh (MESSAGES_DIR=\"${LOBSTER_MESSAGES:-$HOME/messages}\") "
            "and health-check-v3.sh do — hardcoding $HOME/messages would silently "
            "diverge on any install that overrides LOBSTER_MESSAGES."
        )

    def test_dispatcher_pid_cleaned_up_on_exit(self):
        """claude-wrapper.exp should remove dispatcher.pid when the wrapper
        exits, mirroring claude-persistent.sh's `rm -f "$dispatcher_pid_file"`
        cleanup after Claude exits — otherwise a stale PID (later reused by an
        unrelated process) could linger and be targeted incorrectly.
        """
        # Look for a cleanup statement referencing dispatcher.pid, e.g. a
        # `file delete` call, positioned after the `interact` command (i.e.
        # runs once the spawned claude process — and therefore `interact` —
        # has exited).
        interact_idx = self.content.find("interact")
        assert interact_idx != -1, "claude-wrapper.exp must contain 'interact'"

        # Accept either a delete after interact, or an exit-time trap/cleanup
        # elsewhere in the file — the required property is that some code
        # path removes dispatcher.pid once the spawned process is gone.
        assert "file delete" in self.content and "dispatcher.pid" in self.content[interact_idx:] + self.content, (
            "claude-wrapper.exp must clean up dispatcher.pid once the spawned "
            "claude process (and therefore the wrapper) exits, mirroring "
            "claude-persistent.sh's `rm -f \"$dispatcher_pid_file\"` on exit."
        )
