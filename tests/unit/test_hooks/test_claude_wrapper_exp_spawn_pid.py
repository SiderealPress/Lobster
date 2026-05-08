"""
Tests verifying that claude-wrapper.exp writes a valid PID to the dispatcher
startup flag file (regression test for the $spawn_pid vs [exp_pid] bug).

Root cause (fixed in this PR):
  In Tcl/expect, $spawn_pid does not exist as a variable — it is silently
  unset. When claude-wrapper.exp used `puts $fh $spawn_pid`, the catch block
  swallowed the "can't read spawn_pid: no such variable" error, leaving the
  file at 0 bytes (open truncated it; puts never ran). The hook then read the
  empty flag and returned False, injecting the subagent bootup file for every
  debug-mode dispatcher session.

  The correct expect built-in is [exp_pid], which returns the PID of the
  most-recently-spawned process.

These tests use `expect -c` to run Tcl/expect snippets in isolation:
1. Confirm $spawn_pid causes an empty file (the original bug)
2. Confirm [exp_pid] writes a valid PID (the fix)
3. Confirm the actual claude-wrapper.exp script contains [exp_pid], not $spawn_pid
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Path to claude-wrapper.exp — repo root is 3 levels above tests/unit/test_hooks/
_REPO_ROOT = Path(__file__).parents[3]
CLAUDE_WRAPPER_EXP = _REPO_ROOT / "scripts" / "claude-wrapper.exp"

# Guard: skip all tests if expect is not installed on this system.
_EXPECT_AVAILABLE = shutil.which("expect") is not None


@pytest.mark.skipif(not _EXPECT_AVAILABLE, reason="expect not installed")
class TestSpawnPidVsExpPid:
    """Regression tests confirming [exp_pid] writes a valid PID, not an empty file."""

    def test_spawn_pid_variable_does_not_exist_in_tcl(self, tmp_path):
        """$spawn_pid is not a valid Tcl variable in expect after spawn.

        This documents the original bug: using $spawn_pid causes the catch block
        to silently swallow the error and leave the flag file empty.
        """
        flag = tmp_path / "dispatcher-startup-flag"
        # Reproduce the original buggy code: open the file, then try to write $spawn_pid
        script = f"""
spawn sh -c "echo hello"
catch {{
    set fh [open {flag} w]
    puts $fh $spawn_pid
    close $fh
}}
"""
        result = subprocess.run(
            ["expect", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # The file must exist (open creates it) but be empty (puts failed)
        assert flag.exists(), "flag file should have been created by 'open'"
        assert flag.stat().st_size == 0, (
            "flag must be 0 bytes when $spawn_pid is used — "
            "puts fails silently due to undefined variable, original bug"
        )

    def test_exp_pid_writes_valid_integer_pid(self, tmp_path):
        """[exp_pid] correctly writes the spawned process PID to the flag file.

        This is the fix: replacing $spawn_pid with [exp_pid] in claude-wrapper.exp.
        """
        flag = tmp_path / "dispatcher-startup-flag"
        script = f"""
spawn sh -c "echo hello"
catch {{
    set fh [open {flag} w]
    puts $fh [exp_pid]
    close $fh
}}
"""
        result = subprocess.run(
            ["expect", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert flag.exists(), "flag file should exist after [exp_pid] write"
        content = flag.read_text().strip()
        assert content, "flag file must not be empty when [exp_pid] is used"
        assert content.isdigit(), (
            f"flag file must contain a valid integer PID, got: {content!r}"
        )
        pid = int(content)
        assert pid > 0, f"PID must be positive, got: {pid}"

    def test_exp_pid_writes_pid_that_was_alive(self, tmp_path):
        """The PID written by [exp_pid] is a real process PID (was alive at write time).

        We spawn a process that sleeps briefly so we can verify the PID existed.
        After the process exits, kill -0 will fail — we just verify the PID is
        a plausible integer (positive, within range).
        """
        flag = tmp_path / "dispatcher-startup-flag"
        script = f"""
spawn sh -c "sleep 0.1"
catch {{
    set fh [open {flag} w]
    puts $fh [exp_pid]
    close $fh
}}
"""
        result = subprocess.run(
            ["expect", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        content = flag.read_text().strip()
        pid = int(content)
        # Valid PID range on Linux: 1 < pid < 4194304 (PID_MAX_LIMIT)
        assert 1 < pid < 4_194_304, f"PID {pid} is outside valid Linux range"


class TestClaudeWrapperExpContainsFix:
    """Static check: claude-wrapper.exp must use [exp_pid] and not $spawn_pid."""

    def test_script_uses_exp_pid_not_spawn_pid(self):
        """claude-wrapper.exp must reference [exp_pid] for the startup flag write.

        This is a static text check — it ensures the fix is present and cannot
        regress to the broken $spawn_pid form without this test failing.
        """
        assert CLAUDE_WRAPPER_EXP.exists(), (
            f"claude-wrapper.exp not found at {CLAUDE_WRAPPER_EXP}"
        )
        content = CLAUDE_WRAPPER_EXP.read_text()

        # The fix: [exp_pid] must appear in the startup flag write block
        assert "[exp_pid]" in content, (
            "claude-wrapper.exp must use [exp_pid] to write the startup flag PID. "
            "The broken form was 'puts $fh $spawn_pid' — $spawn_pid does not exist "
            "in Tcl/expect, silently leaving the flag file empty."
        )

    def test_script_does_not_use_spawn_pid_variable(self):
        """claude-wrapper.exp must not use $spawn_pid (undefined Tcl variable).

        $spawn_pid is not set by expect's spawn command. Using it causes a silent
        failure: the catch block swallows the error and the flag file is left empty.
        """
        assert CLAUDE_WRAPPER_EXP.exists(), (
            f"claude-wrapper.exp not found at {CLAUDE_WRAPPER_EXP}"
        )
        content = CLAUDE_WRAPPER_EXP.read_text()

        # Filter out comment lines (lines starting with #)
        non_comment_lines = [
            line for line in content.splitlines()
            if not line.strip().startswith("#")
        ]
        non_comment_content = "\n".join(non_comment_lines)

        assert "$spawn_pid" not in non_comment_content, (
            "claude-wrapper.exp must not use $spawn_pid in executable code. "
            "This variable does not exist in Tcl/expect and silently produces "
            "an empty startup flag file, causing the dispatcher to be misidentified "
            "as a subagent and receive the wrong bootup context."
        )
