"""
Unit tests for scripts/reconcile-claude-hooks.py (issue #2249 — the
"disappearing agent problem").

## What this script must do

`hooks/auto-register-agent.py` is the PostToolUse hook that durably records
every spawned background agent into agent_sessions.db and inflight-work.jsonl.
install.sh wires it into a fresh ~/.claude/settings.json idempotently, but
existing git-based installs upgrade via `.githooks/post-merge`, which never
re-runs install.sh's hook-wiring logic. Confirmed live: this hook sat
completely unwired on a running instance, so spawned agents left zero trace
the moment anything went wrong — the "disappearing agent" bug.

`scripts/reconcile-claude-hooks.py` must:
- Detect when a critical hook (starting with auto-register-agent) is missing
  from settings.json's PostToolUse block
- Repair it by appending the canonical hook entry, atomically, idempotently
- Never duplicate an entry that is already present
- Never touch or remove unrelated existing hooks/settings
- Create a minimal settings.json scaffold if the file doesn't exist yet
- Exit 0 when nothing needed repair, 1 when something was repaired,
  2 on fatal error (e.g. invalid JSON)
- Write an inbox system-message alert when it repairs something, so the
  dispatcher can surface the drift to the user

## Named constants (spec-derived, not magic literals)

CRITICAL_HOOK_NAME = "auto-register-agent"
CRITICAL_HOOK_EVENT = "PostToolUse"
CRITICAL_HOOK_COMMAND_MATCH = "auto-register-agent"
EXIT_NOOP = 0
EXIT_REPAIRED = 1
EXIT_FATAL = 2
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Named constants matching the spec / implementation
# ---------------------------------------------------------------------------

CRITICAL_HOOK_NAME = "auto-register-agent"
CRITICAL_HOOK_EVENT = "PostToolUse"
CRITICAL_HOOK_COMMAND_MATCH = "auto-register-agent"
EXIT_NOOP = 0
EXIT_REPAIRED = 1
EXIT_FATAL = 2

# ---------------------------------------------------------------------------
# Path to the script under test / dynamic import of pure functions
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "reconcile-claude-hooks.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reconcile_claude_hooks", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
hook_present = _mod.hook_present
reconcile = _mod.reconcile


def _run_script(settings_path: Path, install_dir: Path, messages_dir: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LOBSTER_CLAUDE_SETTINGS_OVERRIDE": str(settings_path),
        "LOBSTER_INSTALL_DIR": str(install_dir),
        "LOBSTER_WORKSPACE": str(settings_path.parent / "workspace"),
        "LOBSTER_MESSAGES": str(messages_dir),
    }
    return subprocess.run(
        ["uv", "run", str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestHookPresent:
    def test_absent_when_no_hooks_key(self) -> None:
        assert hook_present({}, CRITICAL_HOOK_EVENT, CRITICAL_HOOK_COMMAND_MATCH) is False

    def test_absent_when_event_missing(self) -> None:
        settings = {"hooks": {"PreToolUse": []}}
        assert hook_present(settings, CRITICAL_HOOK_EVENT, CRITICAL_HOOK_COMMAND_MATCH) is False

    def test_absent_when_only_unrelated_hook_present(self) -> None:
        settings = {
            "hooks": {
                CRITICAL_HOOK_EVENT: [
                    {
                        "matcher": "Agent",
                        "hooks": [{"type": "command", "command": "python3 /x/hooks/context-monitor.py"}],
                    }
                ]
            }
        }
        assert hook_present(settings, CRITICAL_HOOK_EVENT, CRITICAL_HOOK_COMMAND_MATCH) is False

    def test_present_when_command_matches(self) -> None:
        settings = {
            "hooks": {
                CRITICAL_HOOK_EVENT: [
                    {
                        "matcher": "Agent",
                        "hooks": [{"type": "command", "command": "python3 /x/hooks/auto-register-agent.py"}],
                    }
                ]
            }
        }
        assert hook_present(settings, CRITICAL_HOOK_EVENT, CRITICAL_HOOK_COMMAND_MATCH) is True


class TestReconcile:
    def test_repairs_missing_hook(self) -> None:
        settings = {"hooks": {}}
        canonical = _mod._canonical_critical_hooks(Path("/opt/lobster"))
        new_settings, repaired = reconcile(settings, canonical)
        assert repaired == [CRITICAL_HOOK_NAME]
        assert hook_present(new_settings, CRITICAL_HOOK_EVENT, CRITICAL_HOOK_COMMAND_MATCH) is True

    def test_noop_when_already_present(self) -> None:
        canonical = _mod._canonical_critical_hooks(Path("/opt/lobster"))
        settings = {"hooks": {CRITICAL_HOOK_EVENT: [canonical[0]["entry"]]}}
        new_settings, repaired = reconcile(settings, canonical)
        assert repaired == []
        # Only one entry present — reconcile must not duplicate it.
        entries = new_settings["hooks"][CRITICAL_HOOK_EVENT]
        matching = [
            e for e in entries
            for h in e.get("hooks", [])
            if CRITICAL_HOOK_COMMAND_MATCH in h.get("command", "")
        ]
        assert len(matching) == 1

    def test_preserves_unrelated_existing_hooks(self) -> None:
        settings = {
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "foo.py"}]}],
                CRITICAL_HOOK_EVENT: [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "context-monitor.py"}]}
                ],
            },
            "env": {"LOBSTER_DEBUG": "true"},
        }
        canonical = _mod._canonical_critical_hooks(Path("/opt/lobster"))
        new_settings, repaired = reconcile(settings, canonical)
        assert repaired == [CRITICAL_HOOK_NAME]
        # Unrelated hooks and top-level keys untouched.
        assert new_settings["hooks"]["PreToolUse"] == settings["hooks"]["PreToolUse"]
        assert new_settings["env"] == {"LOBSTER_DEBUG": "true"}
        # Original PostToolUse entry (context-monitor) still present alongside the new one.
        commands = [
            h.get("command", "")
            for e in new_settings["hooks"][CRITICAL_HOOK_EVENT]
            for h in e.get("hooks", [])
        ]
        assert any("context-monitor" in c for c in commands)
        assert any(CRITICAL_HOOK_COMMAND_MATCH in c for c in commands)

    def test_does_not_mutate_input_settings(self) -> None:
        settings = {"hooks": {}}
        canonical = _mod._canonical_critical_hooks(Path("/opt/lobster"))
        reconcile(settings, canonical)
        assert settings == {"hooks": {}}, "reconcile() must not mutate its input"


# ---------------------------------------------------------------------------
# End-to-end script tests (subprocess, real file I/O)
# ---------------------------------------------------------------------------


class TestScriptEndToEnd:
    def test_repairs_missing_hook_in_settings_file(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {}}))
        install_dir = tmp_path / "lobster"
        messages_dir = tmp_path / "messages"

        result = _run_script(settings_path, install_dir, messages_dir)

        assert result.returncode == EXIT_REPAIRED, f"stderr: {result.stderr}"
        new_settings = json.loads(settings_path.read_text())
        assert hook_present(new_settings, CRITICAL_HOOK_EVENT, CRITICAL_HOOK_COMMAND_MATCH) is True
        # Command path must reference the given install dir.
        commands = [
            h.get("command", "")
            for e in new_settings["hooks"][CRITICAL_HOOK_EVENT]
            for h in e.get("hooks", [])
        ]
        assert any(str(install_dir) in c and "auto-register-agent.py" in c for c in commands)

    def test_noop_exit_code_when_already_wired(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        install_dir = tmp_path / "lobster"
        messages_dir = tmp_path / "messages"
        canonical = _mod._canonical_critical_hooks(install_dir)
        settings_path.write_text(
            json.dumps({"hooks": {CRITICAL_HOOK_EVENT: [canonical[0]["entry"]]}})
        )

        result = _run_script(settings_path, install_dir, messages_dir)

        assert result.returncode == EXIT_NOOP, f"stderr: {result.stderr}"

    def test_running_twice_is_idempotent(self, tmp_path: Path) -> None:
        """Running the script repeatedly must never produce duplicate hook entries."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {}}))
        install_dir = tmp_path / "lobster"
        messages_dir = tmp_path / "messages"

        first = _run_script(settings_path, install_dir, messages_dir)
        second = _run_script(settings_path, install_dir, messages_dir)

        assert first.returncode == EXIT_REPAIRED
        assert second.returncode == EXIT_NOOP

        final_settings = json.loads(settings_path.read_text())
        commands = [
            h.get("command", "")
            for e in final_settings["hooks"][CRITICAL_HOOK_EVENT]
            for h in e.get("hooks", [])
            if CRITICAL_HOOK_COMMAND_MATCH in h.get("command", "")
        ]
        assert len(commands) == 1, f"Expected exactly one entry, got {len(commands)}: {commands}"

    def test_fatal_exit_on_invalid_json(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{ not valid json ")
        install_dir = tmp_path / "lobster"
        messages_dir = tmp_path / "messages"

        result = _run_script(settings_path, install_dir, messages_dir)

        assert result.returncode == EXIT_FATAL, f"stderr: {result.stderr}"
        # Must not have overwritten the corrupt file with something else.
        assert settings_path.read_text() == "{ not valid json "

    def test_creates_settings_scaffold_when_file_absent(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "does-not-exist" / "settings.json"
        install_dir = tmp_path / "lobster"
        messages_dir = tmp_path / "messages"

        result = _run_script(settings_path, install_dir, messages_dir)

        assert result.returncode == EXIT_REPAIRED, f"stderr: {result.stderr}"
        assert settings_path.exists()
        new_settings = json.loads(settings_path.read_text())
        assert hook_present(new_settings, CRITICAL_HOOK_EVENT, CRITICAL_HOOK_COMMAND_MATCH) is True

    def test_writes_inbox_alert_when_hook_repaired(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {}}))
        install_dir = tmp_path / "lobster"
        messages_dir = tmp_path / "messages"
        (messages_dir / "inbox").mkdir(parents=True, exist_ok=True)

        _run_script(settings_path, install_dir, messages_dir)

        inbox_files = list((messages_dir / "inbox").glob("*.json"))
        assert len(inbox_files) == 1, "Expected exactly one inbox alert message"
        alert = json.loads(inbox_files[0].read_text())
        assert alert["source"] == "system"
        assert "auto-register-agent" in alert["text"]
        assert "2249" in alert["text"]

    def test_no_inbox_alert_when_nothing_repaired(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        install_dir = tmp_path / "lobster"
        messages_dir = tmp_path / "messages"
        (messages_dir / "inbox").mkdir(parents=True, exist_ok=True)
        canonical = _mod._canonical_critical_hooks(install_dir)
        settings_path.write_text(
            json.dumps({"hooks": {CRITICAL_HOOK_EVENT: [canonical[0]["entry"]]}})
        )

        _run_script(settings_path, install_dir, messages_dir)

        inbox_files = list((messages_dir / "inbox").glob("*.json"))
        assert inbox_files == []
