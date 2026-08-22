"""
Smoke tests – Post-merge hook rate limiting (.githooks/post-merge)

Why these tests exist:
- PM1: The hook must write an upgrade message to inbox/ when no prior message
  exists (basic functionality).
- PM2: The hook must NOT write a second message if called again within the
  cooldown window (rate-limit correctness — the flood fix).
- PM3: The hook MUST write a new message once the cooldown window has expired.
- PM4/PM5 (issue #2249): the hook must also invoke reconcile-claude-hooks.py
  on every run, so hook-wiring drift self-heals on every `git pull` instead
  of silently persisting forever (the root cause of the "disappearing agent"
  bug — hooks/auto-register-agent.py sat unwired for an unknown period
  because this upgrade path never re-ran install.sh's hook-wiring logic).

`_run_hook` always points `LOBSTER_INSTALL_DIR` at an isolated tmp_path by
default (with no reconcile-claude-hooks.py present) so PM1–PM3 never touch
the real repo or the real ~/.claude/settings.json. PM4/PM5 opt in to a real
copy of the reconcile script under an isolated install dir explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parents[2] / ".githooks" / "post-merge"
RECONCILE_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "reconcile-claude-hooks.py"


def _run_hook(
    inbox_dir: Path,
    state_dir: Path,
    cooldown: int = 600,
    now_ts: int | None = None,
    env_extra: dict | None = None,
    install_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the post-merge hook with overridden paths and optional clock.

    `install_dir` isolates the reconcile-claude-hooks.py lookup (issue #2249)
    — defaults to a tmp dir with no such script present, so by default the
    hook's reconcile step is a silent no-op and cannot touch real production
    paths. Tests exercising the reconcile step pass an install_dir containing
    a real copy of the script plus LOBSTER_CLAUDE_SETTINGS_OVERRIDE.
    """
    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(inbox_dir.parent)
    env["LOBSTER_INSTALL_DIR"] = str(install_dir) if install_dir else str(state_dir / "no-such-lobster-dir")
    # Allow overriding the rate-limit state dir via env (the hook reads
    # LOBSTER_MESSAGES to build STATE_DIR, so we set the parent).
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        env=env,
        capture_output=True,
        text=True,
    )


def _upgrade_messages(inbox_dir: Path) -> list[Path]:
    """Return all JSON files in inbox_dir whose id ends with _upgrade."""
    return [
        p
        for p in inbox_dir.glob("*.json")
        if json.loads(p.read_text()).get("id", "").endswith("_upgrade")
    ]


# ---------------------------------------------------------------------------
# PM1 – writes upgrade message on first run
# ---------------------------------------------------------------------------


def test_post_merge_writes_upgrade_message(tmp_path: Path) -> None:
    """PM1: First invocation must write exactly one upgrade message to inbox/."""
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)

    result = _run_hook(inbox_dir, tmp_path / "config")
    assert result.returncode == 0, f"Hook exited {result.returncode}: {result.stderr}"

    msgs = _upgrade_messages(inbox_dir)
    assert len(msgs) == 1, (
        f"Expected 1 upgrade message after first run, found {len(msgs)}"
    )
    data = json.loads(msgs[0].read_text())
    assert "dependencies may have changed" in data.get("text", ""), (
        f"Unexpected message text: {data.get('text')!r}"
    )


# ---------------------------------------------------------------------------
# PM2 – rate-limited: second call within cooldown is suppressed
# ---------------------------------------------------------------------------


def test_post_merge_rate_limited_within_cooldown(tmp_path: Path) -> None:
    """PM2: Second invocation within cooldown must NOT write another message."""
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)

    # First run — should write.
    result1 = _run_hook(inbox_dir, tmp_path / "config")
    assert result1.returncode == 0

    # Second run immediately — should be suppressed.
    result2 = _run_hook(inbox_dir, tmp_path / "config")
    assert result2.returncode == 0
    assert "Skipping" in result2.stdout, (
        f"Expected 'Skipping' in hook output but got: {result2.stdout!r}"
    )

    msgs = _upgrade_messages(inbox_dir)
    assert len(msgs) == 1, (
        f"Expected exactly 1 upgrade message after two rapid calls (rate-limit "
        f"should suppress second), found {len(msgs)}. Upgrade flood regression."
    )


# ---------------------------------------------------------------------------
# PM3 – writes again once cooldown expires (simulated via stale state file)
# ---------------------------------------------------------------------------


def test_post_merge_writes_after_cooldown_expires(tmp_path: Path) -> None:
    """PM3: After the cooldown window expires, a new message must be written."""
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    # Write a rate-limit state file with a timestamp old enough to be past
    # the cooldown (use epoch 1 = Jan 1 1970 — definitely expired).
    rate_limit_file = config_dir / "upgrade-hook-last-ts"
    rate_limit_file.write_text("1\n")  # epoch second 1 — ancient

    result = _run_hook(inbox_dir, config_dir)
    assert result.returncode == 0

    msgs = _upgrade_messages(inbox_dir)
    assert len(msgs) == 1, (
        f"Expected 1 upgrade message after expired cooldown, found {len(msgs)}"
    )
    assert "Skipping" not in result.stdout, (
        f"Hook should not have skipped after expired cooldown; output: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# PM4/PM5 (issue #2249) — post-merge self-heals hook-wiring drift
# ---------------------------------------------------------------------------


def _make_isolated_install_dir(tmp_path: Path) -> Path:
    """Build a fake install dir containing a real copy of reconcile-claude-hooks.py."""
    install_dir = tmp_path / "lobster"
    (install_dir / "scripts").mkdir(parents=True)
    (install_dir / "scripts" / "reconcile-claude-hooks.py").write_text(
        RECONCILE_SCRIPT_PATH.read_text()
    )
    return install_dir


def test_post_merge_repairs_missing_critical_hook(tmp_path: Path) -> None:
    """PM4: post-merge must repair a missing critical hook in settings.json.

    Reproduces the exact drift that caused issue #2249: a settings.json with
    no auto-register-agent PostToolUse entry. After `git pull` fires this
    hook, the hook must be wired in without any manual install.sh re-run.
    """
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)
    config_dir = tmp_path / "config"
    install_dir = _make_isolated_install_dir(tmp_path)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"hooks": {}}))

    result = _run_hook(
        inbox_dir,
        config_dir,
        install_dir=install_dir,
        env_extra={"LOBSTER_CLAUDE_SETTINGS_OVERRIDE": str(settings_path)},
    )
    assert result.returncode == 0, f"post-merge itself must always exit 0: {result.stderr}"

    new_settings = json.loads(settings_path.read_text())
    commands = [
        h.get("command", "")
        for e in new_settings.get("hooks", {}).get("PostToolUse", [])
        for h in e.get("hooks", [])
    ]
    assert any("auto-register-agent" in c for c in commands), (
        f"Expected auto-register-agent hook to be wired in after post-merge, "
        f"got PostToolUse commands: {commands}"
    )


def test_post_merge_noop_when_reconcile_script_absent(tmp_path: Path) -> None:
    """PM5: when no reconcile script is present (e.g. pre-#2249 checkout),
    post-merge must still succeed and must not create a settings.json."""
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)
    config_dir = tmp_path / "config"
    settings_path = tmp_path / "settings.json"  # deliberately never created

    result = _run_hook(
        inbox_dir,
        config_dir,
        env_extra={"LOBSTER_CLAUDE_SETTINGS_OVERRIDE": str(settings_path)},
    )
    assert result.returncode == 0
    assert not settings_path.exists(), (
        "post-merge must not touch settings.json when reconcile-claude-hooks.py is absent"
    )
