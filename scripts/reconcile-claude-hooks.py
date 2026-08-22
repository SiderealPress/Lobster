#!/usr/bin/env python3
"""reconcile-claude-hooks.py — self-heal drift between the critical hooks this
codebase depends on and what is actually wired into a running instance's
~/.claude/settings.json.

## Why this exists (issue #2249 — "disappearing agent problem")

`hooks/auto-register-agent.py` is a PostToolUse hook that durably records
every spawned background `Agent`/`Task` call into `agent_sessions.db` and
`inflight-work.jsonl` — regardless of whether the dispatcher LLM remembers to
do any manual bookkeeping. `install.sh`'s `setup_claude_hooks()` wires it in
idempotently on a fresh install.

But existing git-based installs upgrade via `.githooks/post-merge`, which
only queues a "run `uv pip install -e .`" reminder after `git pull` — it never
re-runs `install.sh` or any hook-reconciliation step. Any hook added to
`install.sh` after an instance's initial install therefore never reaches that
instance. This was confirmed live: `auto-register-agent.py` sat completely
unwired on a running instance for an unknown period, so every background
agent it spawned left zero trace anywhere the moment something went wrong
(dispatcher restart, LLM skipping the manual fallback bookkeeping step) —
the "disappearing agent" bug.

This script is intentionally NOT a refactor of `install.sh`'s ~1600-line
`setup_claude_hooks()` (too large and unrelated to touch safely here).
Instead it is a small, independent, idempotent reconciler for a short list of
CRITICAL hooks — ones whose absence causes silent, hard-to-detect failures
rather than loud ones — that can run safely and repeatedly against an
ALREADY-DEPLOYED `settings.json` on every dispatcher `SessionStart` and after
every `git pull`, closing the drift gap for good (not just for this one hook
— add future critical hooks to `_canonical_critical_hooks()`).

## Usage

    uv run scripts/reconcile-claude-hooks.py

Exit codes:
    0 — already in sync, nothing repaired
    1 — one or more critical hooks were missing and have been repaired
    2 — fatal error (invalid settings.json, write failure)

## Environment overrides (for tests)

    LOBSTER_CLAUDE_SETTINGS_OVERRIDE — override the settings.json path
    LOBSTER_INSTALL_DIR              — override the lobster install dir (for hook command paths)
    LOBSTER_WORKSPACE                — override the workspace dir (for the reconcile log)
    LOBSTER_MESSAGES                 — override the messages dir (for the inbox alert)
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (env-overridable for testability)
# ---------------------------------------------------------------------------

_HOME = Path(os.environ.get("HOME", str(Path.home())))

INSTALL_DIR = Path(os.environ.get("LOBSTER_INSTALL_DIR", str(_HOME / "lobster")))

SETTINGS_PATH = Path(
    os.environ.get(
        "LOBSTER_CLAUDE_SETTINGS_OVERRIDE",
        str(_HOME / ".claude" / "settings.json"),
    )
)

LOG_PATH = (
    Path(os.environ.get("LOBSTER_WORKSPACE", str(_HOME / "lobster-workspace")))
    / "logs"
    / "hook-reconcile.log"
)

INBOX_DIR = Path(
    os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages"))
) / "inbox"


# ---------------------------------------------------------------------------
# Canonical critical hook manifest (pure data)
# ---------------------------------------------------------------------------


def _canonical_critical_hooks(install_dir: Path) -> list[dict]:
    """Return the canonical list of critical hooks this codebase depends on.

    Pure function of install_dir — no I/O. Intentionally a SMALL, high-value
    subset, not a mirror of install.sh's full hook set: install.sh remains the
    source of truth for fresh installs. Add an entry here only when its
    absence causes a *silent* failure (no error, no trace) rather than a loud
    one — that's the class of bug this script exists to close.
    """
    return [
        {
            "name": "auto-register-agent",
            "event": "PostToolUse",
            "command_match": "auto-register-agent",
            "entry": {
                "matcher": "Agent",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 {install_dir}/hooks/auto-register-agent.py",
                        "timeout": 10,
                    }
                ],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def hook_present(settings: dict, event: str, command_match: str) -> bool:
    """Return True if any hook entry for `event` has a command containing
    `command_match`. Pure function — no I/O, does not mutate settings."""
    for block in settings.get("hooks", {}).get(event, []) or []:
        for h in block.get("hooks", []) or []:
            if command_match in (h.get("command") or ""):
                return True
    return False


def reconcile(settings: dict, canonical_hooks: list[dict]) -> tuple[dict, list[str]]:
    """Return (new_settings, repaired_names).

    For each canonical hook missing from `settings`, appends its entry to the
    appropriate event list. Pure function — does not mutate the input dict;
    returns a new dict. Existing hook entries (including unrelated ones) are
    left untouched.
    """
    new_settings = copy.deepcopy(settings)
    new_settings.setdefault("hooks", {})

    repaired: list[str] = []
    for hook in canonical_hooks:
        event = hook["event"]
        if hook_present(new_settings, event, hook["command_match"]):
            continue
        new_settings["hooks"].setdefault(event, [])
        new_settings["hooks"][event].append(hook["entry"])
        repaired.append(hook["name"])

    return new_settings, repaired


# ---------------------------------------------------------------------------
# I/O functions (isolated side effects)
# ---------------------------------------------------------------------------


def load_settings(path: Path) -> dict:
    """Load settings.json, or return a minimal scaffold if absent.

    Raises json.JSONDecodeError if the file exists but is not valid JSON —
    callers must not silently overwrite a corrupt file.
    """
    if not path.exists():
        return {"hooks": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def write_settings_atomic(path: Path, settings: dict) -> None:
    """Write settings atomically: temp file in the same dir + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".settings-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _log(message: str) -> None:
    """Append a timestamped line to the reconcile log. Never raises."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:  # noqa: BLE001
        pass


def alert_inbox(repaired: list[str]) -> None:
    """Write a system inbox message so the dispatcher can surface the repair.

    Best-effort only — silently does nothing if the messages dir doesn't
    exist (e.g. running in a test/container context) or on any write error.
    """
    try:
        if not INBOX_DIR.parent.exists():
            return
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
        msg_id = f"{ts_ms}_hook_reconcile"
        message = {
            "id": msg_id,
            "source": "system",
            "chat_id": 0,
            "type": "system",
            "text": (
                "System notice: critical Claude Code hook(s) were missing from "
                "~/.claude/settings.json and have been auto-repaired: "
                f"{', '.join(repaired)}. Background agent spawns before this "
                "repair may not have been reliably tracked — see issue #2249 "
                "(the disappearing agent problem)."
            ),
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        }
        (INBOX_DIR / f"{msg_id}.json").write_text(
            json.dumps(message, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        settings = load_settings(SETTINGS_PATH)
    except json.JSONDecodeError as exc:
        msg = f"fatal: {SETTINGS_PATH} is not valid JSON: {exc}"
        _log(msg)
        print(f"[reconcile-claude-hooks] {msg}", file=sys.stderr)
        return 2

    canonical = _canonical_critical_hooks(INSTALL_DIR)
    new_settings, repaired = reconcile(settings, canonical)

    if not repaired:
        return 0

    try:
        write_settings_atomic(SETTINGS_PATH, new_settings)
    except Exception as exc:  # noqa: BLE001
        msg = f"fatal: failed to write {SETTINGS_PATH}: {exc}"
        _log(msg)
        print(f"[reconcile-claude-hooks] {msg}", file=sys.stderr)
        return 2

    msg = f"repaired missing critical hooks: {repaired}"
    _log(msg)
    print(f"[reconcile-claude-hooks] {msg}", file=sys.stderr)
    alert_inbox(repaired)
    return 1


if __name__ == "__main__":
    sys.exit(main())
