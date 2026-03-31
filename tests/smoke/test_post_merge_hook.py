"""
Smoke tests – Post-merge hook rate limiting (.githooks/post-merge)

Why these tests exist:
- PM1: The hook must write an upgrade message to inbox/ when no prior message
  exists (basic functionality).
- PM2: The hook must NOT write a second message if called again within the
  cooldown window (rate-limit correctness — the flood fix).
- PM3: The hook MUST write a new message once the cooldown window has expired.
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


def _run_hook(
    inbox_dir: Path,
    state_dir: Path,
    cooldown: int = 600,
    now_ts: int | None = None,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run the post-merge hook with overridden paths and optional clock."""
    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(inbox_dir.parent)
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
