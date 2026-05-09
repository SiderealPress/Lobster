"""
Regression test: sys.dispatcher.bootup.md uses a single-pass Read (issue #1996).

Before issue #1996, the file contained a "Two-pass read required" notice that
instructed the dispatcher to make two serial Read calls at startup (limit=150,
then offset=150 limit=200). This caused unnecessary startup latency because the
two calls were sequential and nothing else could proceed until both completed.

The fix: replace with a single Read call using limit=350. This test ensures
the two-pass instruction does not reappear as a regression.
"""

from __future__ import annotations

from pathlib import Path

# Path to the actual dispatcher bootup file (relative to the repo root).
_REPO_ROOT = Path(__file__).parents[3]
_DISPATCHER_BOOTUP = _REPO_ROOT / ".claude" / "sys.dispatcher.bootup.md"


def test_bootup_file_exists() -> None:
    """Sanity check: the dispatcher bootup file must exist."""
    assert _DISPATCHER_BOOTUP.exists(), (
        f"sys.dispatcher.bootup.md not found at {_DISPATCHER_BOOTUP}"
    )


def test_no_two_pass_read_instruction() -> None:
    """The two-pass read instruction must not be present (regression guard for #1996).

    The old instruction read:
      "Two-pass read required. ... limit=150 ... offset=150, limit=200"

    This created two serial Read calls on the startup critical path. The fix
    consolidated them into a single call (limit=350).
    """
    content = _DISPATCHER_BOOTUP.read_text()
    assert "Two-pass read required" not in content, (
        "sys.dispatcher.bootup.md still contains the old 'Two-pass read required' "
        "instruction. Issue #1996 replaced this with a single-pass read (limit=350). "
        "Remove the two-pass notice to fix this regression."
    )
    assert "Pass 1:" not in content, (
        "sys.dispatcher.bootup.md still contains a 'Pass 1:' instruction from the "
        "old two-pass read pattern (issue #1996). Replace with single-pass read."
    )
    assert "Pass 2:" not in content, (
        "sys.dispatcher.bootup.md still contains a 'Pass 2:' instruction from the "
        "old two-pass read pattern (issue #1996). Replace with single-pass read."
    )


def test_single_pass_read_instruction_present() -> None:
    """The single-pass read instruction (limit=350) must be present (issue #1996).

    Ensures the replacement instruction was actually added after removing the
    two-pass notice.
    """
    content = _DISPATCHER_BOOTUP.read_text()
    assert "limit=350" in content, (
        "sys.dispatcher.bootup.md does not contain the single-pass read instruction "
        "'limit=350'. Issue #1996 requires a single Read call with limit=350 to "
        "replace the old two-pass pattern."
    )
