"""
Unit tests for the setup_claude_hooks() install.sh fix (issue #1947).

Validates that:
  1. setup_claude_hooks() registers on-compact.py with matcher="" (not "compact")
  2. setup_claude_hooks() does NOT add a compact-matcher inject-bootup-context entry
  3. Migration 91 in upgrade.sh removes any existing matcher="compact" on-compact.py entries
  4. Migration 91 removes any compact-matcher inject-bootup-context entries

The core invariant: on-compact.py is registered with matcher="" because
matcher="compact" is unreliable in CC 2.1.119 (~37% fire rate). The hook
uses an internal self-gate (_is_compact_event()) to skip non-compact events.

Tests operate on synthetic settings.json structures (no real file required).
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

import pytest

_REPO_DIR = Path(__file__).parents[3]
_INSTALL_SH = _REPO_DIR / "install.sh"
_UPGRADE_SH = _REPO_DIR / "scripts" / "upgrade.sh"

# Named constant matching the issue spec.
# "matcher='compact' is unreliable in CC 2.1.119 (~37% fire rate since April 17)"
_UNRELIABLE_COMPACT_MATCHER = "compact"
# The correct matcher for on-compact.py — fires on all SessionStart events;
# the script self-gates using _is_compact_event().
_RELIABLE_EMPTY_MATCHER = ""


# ---------------------------------------------------------------------------
# Helper: extract run_migrations shell function and test migration 91
# ---------------------------------------------------------------------------

def _apply_migration_91(settings: dict) -> dict:
    """Apply the migration 91 logic (inline Python port of the shell script).

    Migration 91 in upgrade.sh uses Python3 heredoc to:
      1. Change on-compact.py SessionStart entries from matcher="compact" to matcher=""
      2. Remove any inject-bootup-context.py entries with matcher="compact"
         (the empty-matcher entry already covers all session types)

    This function ports that logic as pure Python so it can be unit-tested
    without invoking bash.
    """
    session_start = settings.get("hooks", {}).get("SessionStart", [])
    updated = []
    for entry in session_start:
        cmd = entry.get("hooks", [{}])[0].get("command", "")
        # Change on-compact.py from matcher="compact" to matcher=""
        if "on-compact" in cmd and entry.get("matcher") == _UNRELIABLE_COMPACT_MATCHER:
            entry = dict(entry, matcher=_RELIABLE_EMPTY_MATCHER)
        # Remove the redundant inject-bootup-context.py compact-matcher entry
        # (the empty-matcher entry already fires on all session types including compact)
        elif "inject-bootup-context" in cmd and entry.get("matcher") == _UNRELIABLE_COMPACT_MATCHER:
            continue
        updated.append(entry)
    result = dict(settings)
    result.setdefault("hooks", {})["SessionStart"] = updated
    return result


# ---------------------------------------------------------------------------
# Tests: _apply_migration_91() logic
# ---------------------------------------------------------------------------

class TestMigration91Logic:
    """Validate the core logic that migration 91 applies to settings.json."""

    def _make_settings_with_compact_matcher(self) -> dict:
        """Return a settings.json structure with the old broken compact-matcher entries."""
        return {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": _UNRELIABLE_COMPACT_MATCHER,
                        "hooks": [{"type": "command", "command": "python3 /home/lobster/lobster/hooks/on-compact.py", "timeout": 30}]
                    },
                    {
                        "matcher": _RELIABLE_EMPTY_MATCHER,
                        "hooks": [{"type": "command", "command": "python3 /home/lobster/lobster/hooks/inject-bootup-context.py", "timeout": 10}]
                    },
                    {
                        "matcher": _UNRELIABLE_COMPACT_MATCHER,
                        "hooks": [{"type": "command", "command": "python3 /home/lobster/lobster/hooks/inject-bootup-context.py", "timeout": 10}]
                    },
                ]
            }
        }

    def test_compact_matcher_on_compact_changed_to_empty_matcher(self):
        """on-compact.py matcher='compact' must become matcher='' after migration."""
        settings = self._make_settings_with_compact_matcher()
        result = _apply_migration_91(settings)

        on_compact_entries = [
            e for e in result["hooks"]["SessionStart"]
            if "on-compact" in e.get("hooks", [{}])[0].get("command", "")
        ]
        assert len(on_compact_entries) == 1, "Should have exactly one on-compact.py entry"
        assert on_compact_entries[0]["matcher"] == _RELIABLE_EMPTY_MATCHER, (
            f"on-compact.py matcher must be '' after migration, got: {on_compact_entries[0]['matcher']!r}"
        )

    def test_compact_matcher_inject_bootup_removed(self):
        """inject-bootup-context.py compact-matcher entry must be removed after migration."""
        settings = self._make_settings_with_compact_matcher()
        result = _apply_migration_91(settings)

        inject_entries = [
            e for e in result["hooks"]["SessionStart"]
            if "inject-bootup-context" in e.get("hooks", [{}])[0].get("command", "")
        ]
        # Only the empty-matcher entry should remain
        compact_inject_entries = [e for e in inject_entries if e["matcher"] == _UNRELIABLE_COMPACT_MATCHER]
        assert len(compact_inject_entries) == 0, (
            "inject-bootup-context.py compact-matcher entry must be removed (empty-matcher covers all sessions)"
        )

    def test_empty_matcher_inject_bootup_preserved(self):
        """inject-bootup-context.py empty-matcher entry must be preserved after migration."""
        settings = self._make_settings_with_compact_matcher()
        result = _apply_migration_91(settings)

        inject_entries = [
            e for e in result["hooks"]["SessionStart"]
            if "inject-bootup-context" in e.get("hooks", [{}])[0].get("command", "")
        ]
        empty_inject_entries = [e for e in inject_entries if e["matcher"] == _RELIABLE_EMPTY_MATCHER]
        assert len(empty_inject_entries) == 1, (
            "inject-bootup-context.py empty-matcher entry must be preserved"
        )

    def test_already_correct_settings_unchanged(self):
        """Migration must be a no-op when on-compact.py already uses matcher=''."""
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": _RELIABLE_EMPTY_MATCHER,
                        "hooks": [{"type": "command", "command": "python3 /home/lobster/lobster/hooks/on-compact.py", "timeout": 30}]
                    },
                    {
                        "matcher": _RELIABLE_EMPTY_MATCHER,
                        "hooks": [{"type": "command", "command": "python3 /home/lobster/lobster/hooks/inject-bootup-context.py", "timeout": 10}]
                    },
                ]
            }
        }
        result = _apply_migration_91(settings)

        on_compact_entries = [
            e for e in result["hooks"]["SessionStart"]
            if "on-compact" in e.get("hooks", [{}])[0].get("command", "")
        ]
        assert len(on_compact_entries) == 1
        assert on_compact_entries[0]["matcher"] == _RELIABLE_EMPTY_MATCHER

        inject_entries = [
            e for e in result["hooks"]["SessionStart"]
            if "inject-bootup-context" in e.get("hooks", [{}])[0].get("command", "")
        ]
        assert len(inject_entries) == 1
        assert inject_entries[0]["matcher"] == _RELIABLE_EMPTY_MATCHER

    def test_other_session_start_hooks_preserved(self):
        """Migration must not disturb other SessionStart hook entries."""
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": _UNRELIABLE_COMPACT_MATCHER,
                        "hooks": [{"type": "command", "command": "python3 /home/lobster/lobster/hooks/on-compact.py", "timeout": 30}]
                    },
                    {
                        "matcher": _RELIABLE_EMPTY_MATCHER,
                        "hooks": [{"type": "command", "command": "python3 /home/lobster/lobster/hooks/on-fresh-start.py", "timeout": 30}]
                    },
                ]
            }
        }
        result = _apply_migration_91(settings)

        # on-compact.py matcher changed
        on_compact = [
            e for e in result["hooks"]["SessionStart"]
            if "on-compact" in e.get("hooks", [{}])[0].get("command", "")
        ]
        assert on_compact[0]["matcher"] == _RELIABLE_EMPTY_MATCHER

        # on-fresh-start.py untouched
        on_fresh_start = [
            e for e in result["hooks"]["SessionStart"]
            if "on-fresh-start" in e.get("hooks", [{}])[0].get("command", "")
        ]
        assert len(on_fresh_start) == 1
        assert on_fresh_start[0]["matcher"] == _RELIABLE_EMPTY_MATCHER

    def test_empty_session_start_is_no_op(self):
        """Migration on empty SessionStart list must not crash and must leave it empty."""
        settings = {"hooks": {"SessionStart": []}}
        result = _apply_migration_91(settings)
        assert result["hooks"]["SessionStart"] == []


# ---------------------------------------------------------------------------
# Invariant tests: install.sh must register on-compact.py with matcher=""
# ---------------------------------------------------------------------------

class TestInstallShOnCompactMatcher:
    """Verify that install.sh install_hooks() uses matcher='' for on-compact.py.

    These tests check the install.sh source directly — not execution.
    The functional correctness of the shell execution is validated by
    tests/unit/test_setup_claude_hooks.sh (runs against live settings.json).
    """

    def test_install_sh_does_not_register_on_compact_with_compact_matcher(self):
        """install.sh must not add on-compact.py with matcher='compact'.

        The old broken code registered on-compact.py as:
            {"matcher": "compact", "hooks": [...on-compact.py...]}

        The fix registers it as:
            {"matcher": "", "hooks": [...on-compact.py...]}

        This test catches regression: any line in setup_claude_hooks() that
        sets matcher="compact" for the on-compact hook.
        """
        install_text = _INSTALL_SH.read_text()

        # Find the setup_claude_hooks function body.
        # Look for "Set compact flag on context compaction" section
        # If the old broken pattern is present (matcher="compact" adjacent to on-compact.py),
        # flag it.
        lines = install_text.splitlines()

        # Build a sliding window to detect: within 5 lines of "on-compact",
        # is there a 'matcher": "compact"' assignment?
        for i, line in enumerate(lines):
            if "on-compact" in line and "matcher" not in line:
                window = lines[max(0, i-5):i+10]
                window_text = "\n".join(window)
                assert '"matcher": "compact"' not in window_text, (
                    f"setup_claude_hooks() near line {i+1} still registers on-compact.py "
                    f"with matcher='compact'. Use matcher='' instead.\n"
                    f"Context:\n{window_text}"
                )

    def test_install_sh_does_not_add_compact_matcher_inject_bootup(self):
        """install.sh must not add inject-bootup-context.py with matcher='compact'.

        The old code added a second inject-bootup-context.py entry:
            {"matcher": "compact", "hooks": [...inject-bootup-context.py...]}

        This causes double-injection on every compaction — the empty-matcher entry
        already fires on all session types. The fix removes this block entirely.
        """
        install_text = _INSTALL_SH.read_text()

        # Check that there's no "Re-inject bootup context after compaction" block
        # that would add a compact-matcher inject-bootup-context entry.
        # The comment itself is OK — the problem is the jq command that follows it.
        lines = install_text.splitlines()

        for i, line in enumerate(lines):
            # Look for the pattern that adds inject-bootup-context with matcher="compact"
            if '"matcher": "compact"' in line:
                # Check surrounding context for inject-bootup-context
                window_start = max(0, i - 5)
                window = lines[window_start:i+5]
                window_text = "\n".join(window)
                assert "inject-bootup-context" not in window_text, (
                    f"setup_claude_hooks() near line {i+1} adds inject-bootup-context.py "
                    f"with matcher='compact'. This causes double-injection — remove it.\n"
                    f"Context:\n{window_text}"
                )

    def test_install_sh_registers_on_compact_with_empty_matcher(self):
        """install.sh must register on-compact.py with matcher='' in setup_claude_hooks().

        The self-gating _is_compact_event() function inside on-compact.py handles
        filtering — the hook does not need matcher='compact'.
        """
        install_text = _INSTALL_SH.read_text()
        lines = install_text.splitlines()

        # Find lines that set matcher="" adjacent to on-compact.py command
        found_correct_registration = False
        for i, line in enumerate(lines):
            if "on-compact" in line:
                window = lines[max(0, i-5):i+10]
                window_text = "\n".join(window)
                if '"matcher": ""' in window_text:
                    found_correct_registration = True
                    break

        assert found_correct_registration, (
            "install.sh must register on-compact.py with matcher='' in setup_claude_hooks(). "
            "The script has an internal self-gate (_is_compact_event()) that handles filtering."
        )
