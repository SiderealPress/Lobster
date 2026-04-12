"""
Tests for scripts/lobster-observe.py

Verifies:
1. build_observation_payload produces the correct shape
2. write_observation_to_inbox writes atomically to the right path
3. CLI produces a subagent_observation with the expected fields
4. system_error observations write to observations.log (durability fallback)
5. Non-system_error observations do NOT write to observations.log
6. No writes to outbox/ (inbox-only alerting)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).parent.parent.parent
HELPER = REPO_DIR / "scripts" / "lobster-observe.py"


# ---------------------------------------------------------------------------
# Unit tests (pure functions)
# ---------------------------------------------------------------------------


def test_build_observation_payload_shape():
    """build_observation_payload returns a dict with required fields."""
    sys.path.insert(0, str(REPO_DIR / "scripts"))
    # Import the module by loading the file directly to avoid name collision
    import importlib.util
    spec = importlib.util.spec_from_file_location("lobster_observe", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    payload = mod.build_observation_payload(
        text="test message",
        category="system_error",
        chat_id=0,
        source="system",
        task_id="task-123",
    )

    assert payload["type"] == "subagent_observation"
    assert payload["category"] == "system_error"
    assert payload["text"] == "test message"
    assert payload["chat_id"] == 0
    assert payload["source"] == "system"
    assert payload["task_id"] == "task-123"
    assert "_observation_" in payload["id"]
    assert "timestamp" in payload


def test_build_observation_payload_no_task_id():
    """task_id is omitted from payload when not provided."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lobster_observe", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    payload = mod.build_observation_payload(
        text="msg",
        category="system_context",
        chat_id=0,
        source="system",
    )
    assert "task_id" not in payload


def test_write_observation_to_inbox_atomic(tmp_path):
    """write_observation_to_inbox writes an atomically-renamed JSON file."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lobster_observe", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    inbox_dir = tmp_path / "inbox"
    payload = mod.build_observation_payload(
        text="hello",
        category="system_error",
        chat_id=0,
        source="system",
    )
    mod.write_observation_to_inbox(inbox_dir, payload)

    written = list(inbox_dir.glob("*_observation_*.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["type"] == "subagent_observation"
    assert data["text"] == "hello"

    # No leftover .tmp files
    tmp_files = list(inbox_dir.glob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def _run_helper(args: list[str], env: dict) -> subprocess.CompletedProcess:
    # Use uv if available (local dev), fall back to sys.executable (Docker CI)
    import shutil
    if shutil.which("uv"):
        cmd = ["uv", "run", str(HELPER)] + args
    else:
        cmd = [sys.executable, str(HELPER)] + args
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(REPO_DIR),
    )


def test_cli_writes_observation_to_inbox(tmp_path):
    """Running the helper via CLI writes a subagent_observation file to inbox."""
    import os
    messages_dir = tmp_path / "messages"
    inbox_dir = messages_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(messages_dir)

    result = _run_helper(
        ["--category", "system_error", "--text", "Job foo was auto-disabled."],
        env=env,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    obs_files = list(inbox_dir.glob("*_observation_*.json"))
    assert len(obs_files) == 1, f"Expected 1 observation file, got {len(obs_files)}"
    payload = json.loads(obs_files[0].read_text())
    assert payload["type"] == "subagent_observation"
    assert payload["category"] == "system_error"
    assert "foo" in payload["text"]


def test_cli_system_error_writes_observations_log(tmp_path):
    """system_error observations must be written to observations.log as a durability fallback."""
    import os
    messages_dir = tmp_path / "messages"
    workspace_dir = tmp_path / "workspace"
    inbox_dir = messages_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(messages_dir)
    env["LOBSTER_WORKSPACE"] = str(workspace_dir)

    result = _run_helper(
        ["--category", "system_error", "--text", "Test alert."],
        env=env,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    obs_log = workspace_dir / "logs" / "observations.log"
    assert obs_log.exists(), "observations.log must be written for system_error"
    import json as _json
    lines = [_json.loads(l) for l in obs_log.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["category"] == "system_error"
    assert entry["source"] == "cron-direct"
    assert "Test alert" in entry["content"]


def test_cli_non_system_error_does_not_write_observations_log(tmp_path):
    """Non-system_error observations must NOT write to observations.log."""
    import os
    messages_dir = tmp_path / "messages"
    workspace_dir = tmp_path / "workspace"
    inbox_dir = messages_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "logs").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(messages_dir)
    env["LOBSTER_WORKSPACE"] = str(workspace_dir)

    _run_helper(
        ["--category", "system_context", "--text", "Informational note."],
        env=env,
    )

    obs_log = workspace_dir / "logs" / "observations.log"
    assert not obs_log.exists(), "observations.log must not be written for non-system_error categories"


def test_cli_no_outbox_written(tmp_path):
    """Helper must not write to outbox/ — alerting is inbox-only."""
    import os
    messages_dir = tmp_path / "messages"
    inbox_dir = messages_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(messages_dir)

    _run_helper(
        ["--category", "system_error", "--text", "Test alert."],
        env=env,
    )

    outbox_files = list((messages_dir / "outbox").glob("*.json")) if (messages_dir / "outbox").exists() else []
    assert outbox_files == [], f"Helper must not write outbox files, found: {outbox_files}"


def test_cli_invalid_category_exits_nonzero(tmp_path):
    """Invalid --category must exit non-zero."""
    import os
    messages_dir = tmp_path / "messages"
    (messages_dir / "inbox").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LOBSTER_MESSAGES"] = str(messages_dir)

    result = _run_helper(
        ["--category", "invalid_category", "--text", "oops"],
        env=env,
    )
    assert result.returncode != 0, "Expected non-zero exit for invalid category"
